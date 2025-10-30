import asyncio
import builtins
import io
import json
import os
import subprocess
import threading
import time
import re
import shutil
import sys
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict
from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify

# --- Configuration ---
# !! IMPORTANT: Update this list with your Pi Nodes' static IP addresses !!
PI_NODES = [
    '192.168.0.101',
    '192.168.0.102',
    '192.168.0.103',
]
PORT = 9090  # Port specified in pi_node_listener.py
DOWNLOAD_PATH_BASE = os.path.expanduser("~/video_downloads") # Local folder to save files
REMOTE_VIDEO_PATH = "/home/pi/videos/"  # Path on the Pi Nodes
REMOTE_PI_USER = "pi"  # Username on the Pi Nodes
# ---------------------

app = Flask(__name__)
# A secret key is required for flashing messages
app.secret_key = os.urandom(24)

# --- Logging Capture ---

LOG_BUFFER = deque(maxlen=500)
LOG_LOCK = threading.Lock()
_original_print = builtins.print


def _capture_print(*args, **kwargs):
    """Capture stdout prints so they can be streamed to the UI."""
    file_obj = kwargs.get("file", sys.stdout)

    # If printing to something other than stdout (e.g., a file), do not capture.
    if file_obj not in (None, sys.stdout):
        return _original_print(*args, **kwargs)

    sep = kwargs.get("sep", " ")
    end = kwargs.get("end", "\n")
    message = sep.join(str(arg) for arg in args) + end

    with LOG_LOCK:
        LOG_BUFFER.append({
            "timestamp": datetime.now().isoformat(),
            "message": message.rstrip("\n")
        })

    _original_print(*args, **kwargs)


builtins.print = _capture_print


# --- Node Status Tracking ---


@dataclass
class NodeStatus:
    ip: str
    reachable: bool = False
    recording: bool = False
    take_name: str = ""
    last_response: str = ""
    last_error: str = ""
    last_command: str = ""
    last_updated: float = 0.0
    last_heartbeat: float = 0.0

    def to_dict(self) -> Dict:
        last_updated_iso = (
            datetime.fromtimestamp(self.last_updated).isoformat()
            if self.last_updated else None
        )
        last_heartbeat_iso = (
            datetime.fromtimestamp(self.last_heartbeat).isoformat()
            if self.last_heartbeat else None
        )
        heartbeat_age = (
            max(time.time() - self.last_heartbeat, 0.0)
            if self.last_heartbeat else None
        )
        return {
            "ip": self.ip,
            "reachable": self.reachable,
            "recording": self.recording,
            "take_name": self.take_name,
            "last_response": self.last_response,
            "last_error": self.last_error,
            "last_command": self.last_command,
            "last_updated": last_updated_iso,
            "last_heartbeat": last_heartbeat_iso,
            "heartbeat_age": heartbeat_age,
        }


node_status_lock = threading.Lock()
node_statuses: Dict[str, NodeStatus] = {ip: NodeStatus(ip=ip) for ip in PI_NODES}
STATUS_POLL_INTERVAL_SECONDS = 5
status_monitor_started = threading.Event()


def update_node_status(ip: str, update_timestamp: bool = True, **fields) -> None:
    """Update the cached status for a node in a threadsafe way."""
    with node_status_lock:
        status = node_statuses.setdefault(ip, NodeStatus(ip=ip))
        for key, value in fields.items():
            setattr(status, key, value)
        if update_timestamp:
            status.last_updated = time.time()


def get_node_status_snapshot() -> Dict[str, Dict]:
    with node_status_lock:
        return {ip: status.to_dict() for ip, status in node_statuses.items()}


async def poll_all_statuses():
    """Request STATUS from all nodes concurrently."""
    if not PI_NODES:
        return
    tasks = [send_command(ip, 'STATUS', silent=True) for ip in PI_NODES]
    await asyncio.gather(*tasks, return_exceptions=True)


def _status_polling_loop():
    """Background loop that keeps node heartbeats fresh."""
    while True:
        try:
            asyncio.run(poll_all_statuses())
        except Exception as exc:
            print(f"[{datetime.now().isoformat()}] [Warning] Status polling error: {exc}")
        time.sleep(STATUS_POLL_INTERVAL_SECONDS)


def ensure_status_monitor_running():
    if status_monitor_started.is_set():
        return
    thread = threading.Thread(target=_status_polling_loop, daemon=True, name="NodeStatusMonitor")
    thread.start()
    status_monitor_started.set()


# --- Download Status Tracking ---

download_status_lock = threading.Lock()
download_status = {
    "state": "idle",
    "message": "No downloads in progress.",
    "in_progress": False,
    "last_updated": datetime.now().isoformat(),
    "last_success": None,
}


def update_download_status(state: str, message: str, *, in_progress: bool) -> None:
    timestamp = datetime.now().isoformat()
    with download_status_lock:
        download_status.update({
            "state": state,
            "message": message,
            "in_progress": in_progress,
            "last_updated": timestamp,
        })
        if state == "success":
            download_status["last_success"] = timestamp


def get_download_status_snapshot() -> Dict:
    with download_status_lock:
        return dict(download_status)


def get_logs_snapshot(limit: int = 300):
    with LOG_LOCK:
        if not LOG_BUFFER:
            return []
        actual_limit = max(1, min(limit, len(LOG_BUFFER)))
        return list(LOG_BUFFER)[-actual_limit:]

# --- Asynchronous Command Functions ---

async def send_command(ip, command, silent: bool = False):
    """Asynchronously sends a command to a single Pi node."""
    update_node_status(ip, update_timestamp=False, last_command=command)

    try:
        if not silent:
            print(f"[{datetime.now().isoformat()}] Sending {command} to {ip}...")

        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, PORT),
            timeout=5.0
        )

        writer.write(command.encode('utf-8'))
        await writer.drain()

        response = await asyncio.wait_for(reader.read(256), timeout=2.0)
        response_text = response.decode('utf-8', errors='ignore')

        if not silent:
            print(f"[{datetime.now().isoformat()}] [Response from {ip}]: {response_text}")

        writer.close()
        await writer.wait_closed()

        update_node_status(
            ip,
            reachable=True,
            last_response=response_text,
            last_error="",
        )

        if command.startswith('START:') and 'ACK_START' in response_text:
            take_name = command.split(':', 1)[1]
            update_node_status(ip, recording=True, take_name=take_name)
        elif command == 'STOP' and 'ACK_STOP' in response_text:
            update_node_status(ip, recording=False, take_name="")
        elif command == 'STATUS' and response_text.startswith('STATUS:'):
            payload = response_text.split('STATUS:', 1)[1].strip()
            status_data = {}
            if payload:
                try:
                    status_data = json.loads(payload)
                except json.JSONDecodeError:
                    print(f"[{datetime.now().isoformat()}] [Warning] Could not decode STATUS payload from {ip}: {payload}")
            heartbeat_time = time.time()
            update_node_status(
                ip,
                recording=bool(status_data.get('recording', False)),
                take_name=status_data.get('take_name') or "",
                last_error=status_data.get('last_error', ''),
                last_response=response_text,
                last_heartbeat=heartbeat_time,
            )
        else:
            # Keep reachable flag on any successful response
            update_node_status(ip, reachable=True)

    except asyncio.TimeoutError:
        if not silent:
            print(f"[{datetime.now().isoformat()}] [Error] Timeout connecting or communicating with {ip}.")
        update_node_status(ip, reachable=False, last_error="Timeout communicating with node.")
    except Exception as e:
        if not silent:
            print(f"[{datetime.now().isoformat()}] [Error] Failed to send command to {ip}: {e}")
        update_node_status(ip, reachable=False, last_error=str(e))

async def broadcast_command(command):
    """Sends a command to all Pi nodes in parallel."""
    print(f"\n--- Broadcasting command: {command} ---")
    tasks = [send_command(ip, command) for ip in PI_NODES]
    await asyncio.gather(*tasks)
    print("--- Broadcast complete ---")


async def broadcast_shell_command(remote_command):
    """Run a shell command on all nodes concurrently."""
    tasks = [run_ssh_command(ip, remote_command) for ip in PI_NODES]
    return await asyncio.gather(*tasks, return_exceptions=True)


async def run_ssh_command(ip, remote_command):
    """Execute a given shell command on a remote Pi via SSH."""
    ssh_cmd = [
        "ssh",
        f"{REMOTE_PI_USER}@{ip}",
        remote_command
    ]
    try:
        print(f"[{datetime.now().isoformat()}] Running on {ip}: {remote_command}")
        proc = await asyncio.create_subprocess_exec(
            *ssh_cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE
        )
        stdout, stderr = await proc.communicate()
        if stdout:
            print(f"[{datetime.now().isoformat()}] [{ip}] STDOUT: {stdout.decode('utf-8', errors='ignore')}")
        if stderr:
            print(f"[{datetime.now().isoformat()}] [{ip}] STDERR: {stderr.decode('utf-8', errors='ignore')}")
        if proc.returncode == 0:
            print(f"[{datetime.now().isoformat()}] [Success] Command succeeded on {ip}.")
        else:
            print(f"[{datetime.now().isoformat()}] [Error] Command failed on {ip} with exit code {proc.returncode}.")
        return proc.returncode
    except Exception as exc:
        print(f"[{datetime.now().isoformat()}] [Error] SSH command failed for {ip}: {exc}")
        return exc

# --- Synchronous Download Function (to be run in a thread) ---

def download_files_threaded():
    """
    Downloads files from all Pi nodes using rsync, then sorts
    them into folders based on their 'take_name'.
    """
    update_download_status('running', 'Preparing download & sort...', in_progress=True)
    print(f"\n[{datetime.now().isoformat()}] --- Starting file download thread ---")
    try:
        os.makedirs(DOWNLOAD_PATH_BASE, exist_ok=True)

        # --- Step 1: Download all files from all nodes ---
        print(f"[{datetime.now().isoformat()}] --- [Step 1] Syncing files via rsync ---")
        update_download_status('running', 'Syncing files from camera nodes...', in_progress=True)
        synced_ips = []
        had_errors = False
        error_messages = []
        for ip in PI_NODES:
            # We download into a temporary folder named after the IP
            # This folder will be sorted from and then deleted
            temp_target_dir = os.path.join(DOWNLOAD_PATH_BASE, f"__temp_{ip}")
            os.makedirs(temp_target_dir, exist_ok=True)

            print(f"\n[{datetime.now().isoformat()}] [Info] Downloading files from {ip} to {temp_target_dir}...")
            update_download_status('running', f'Downloading from {ip}...', in_progress=True)

            cmd = [
                'rsync',
                '-avz', # Archive, verbose, compress
                '--partial', # Keep partially-downloaded files
                # '--remove-source-files', # Uncomment this to delete files from nodes after download
                f"{REMOTE_PI_USER}@{ip}:{REMOTE_VIDEO_PATH}",
                temp_target_dir
            ]

            try:
                print(f"[{datetime.now().isoformat()}] Running: {' '.join(cmd)}")
                subprocess.run(cmd, check=True, capture_output=True, text=True)
                print(f"[{datetime.now().isoformat()}] [Success] Files downloaded from {ip}.")
                synced_ips.append(ip) # Add to list for sorting
            except subprocess.CalledProcessError as e:
                print(f"[{datetime.now().isoformat()}] [Error] Failed to download files from {ip}: {e}")
                print(f"       STDOUT: {e.stdout}")
                print(f"       STDERR: {e.stderr}")
                print("       CRITICAL: Check that SSH keys are set up for passwordless login.")
                had_errors = True
                error_summary = e.stderr.strip() if e.stderr else str(e)
                error_messages.append(f"{ip}: {error_summary}")
            except FileNotFoundError:
                print(f"[{datetime.now().isoformat()}] [Error] rsync command not found. Is it installed?")
                had_errors = True
                error_messages.append("rsync not found on controller")

        # --- Step 2: Sort downloaded files into take_name folders ---
        print(f"\n[{datetime.now().isoformat()}] --- [Step 2] Sorting downloaded files by take_name ---")
        update_download_status('running', 'Sorting downloaded files by take...', in_progress=True)
        for ip in synced_ips:
            source_dir = os.path.join(DOWNLOAD_PATH_BASE, f"__temp_{ip}")
            safe_ip = ip.replace('.', '_')

            update_download_status('running', f'Sorting files from {ip}...', in_progress=True)

            # Regex to extract take_name: (take_name)_(safe_ip)_(timestamp)_...
            # (.+)                     -> Group 1: The take_name (matches one or more chars)
            # _(safe_ip_string)        -> Matches the literal safe_ip
            # _(\d{8}_\d{6})           -> Group 2: The timestamp (YYYYMMDD_HHMMSS)
            # (.*)                     -> Group 3: The rest (e.g., _000000.h264 or _markers.txt)
            filename_regex = re.compile(r'(.+)_' + re.escape(safe_ip) + r'_(\d{8}_\d{6})(.*)')

            try:
                for filename in os.listdir(source_dir):
                    source_path = os.path.join(source_dir, filename)

                    # Skip if it's a directory
                    if os.path.isdir(source_path):
                        continue

                    match = filename_regex.match(filename)

                    if match:
                        take_name = match.group(1)

                        # Create new destination directories
                        # Final path: video_downloads/TAKE_NAME/IP_ADDRESS/filename.h264
                        take_dir = os.path.join(DOWNLOAD_PATH_BASE, take_name)
                        dest_dir = os.path.join(take_dir, ip) # Use the real IP for the folder name
                        os.makedirs(dest_dir, exist_ok=True)

                        # Move the file
                        new_path = os.path.join(dest_dir, filename)

                        try:
                            shutil.move(source_path, new_path)
                            print(f"Moved: {filename} -> {os.path.join(take_name, ip, filename)}")
                        except Exception as move_e:
                            print(f"[{datetime.now().isoformat()}] [Error] Could not move file {source_path}: {move_e}")
                            had_errors = True
                            error_messages.append(f"{ip}: failed to move {filename} -> {move_e}")
                    else:
                        print(f"[{datetime.now().isoformat()}] [Warning] File '{filename}' in {source_dir} did not match expected format. Skipping.")

            except Exception as e:
                print(f"[{datetime.now().isoformat()}] [Error] Failed to sort files for {ip}: {e}")
                had_errors = True
                error_messages.append(f"{ip}: sort error {e}")

            # --- Step 3: Clean up empty source IP directories ---
            try:
                if os.path.exists(source_dir) and not os.listdir(source_dir):
                    os.rmdir(source_dir)
                    print(f"[{datetime.now().isoformat()}] Cleaned up empty directory: {source_dir}")
            except Exception as e:
                print(f"[{datetime.now().isoformat()}] [Error] Could not remove empty directory {source_dir}: {e}")

        print(f"[{datetime.now().isoformat()}] --- File download and sorting thread finished ---")

        if had_errors:
            message = 'Completed with warnings: ' + '; '.join(error_messages)
            update_download_status('warning', message, in_progress=False)
        else:
            finished_msg = f"Download finished successfully at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"
            update_download_status('success', finished_msg, in_progress=False)

    except Exception as exc:
        print(f"[{datetime.now().isoformat()}] [Error] Unexpected failure in download thread: {exc}")
        update_download_status('error', f'Unexpected failure: {exc}', in_progress=False)


# --- Web Application UI (HTML Template) ---

HTML_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Pi Multi-Cam Controller</title>
    <style>
        body {
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            margin: 0;
            padding: 2rem 1rem 3rem;
            background-color: #edf1f5;
            color: #333;
            display: flex;
            justify-content: center;
        }
        .container {
            width: 100%;
            max-width: 720px;
            background: #ffffff;
            border-radius: 16px;
            box-shadow: 0 18px 45px rgba(14, 30, 37, 0.12);
            padding: 2.5rem 2.25rem 2.75rem;
            border: 1px solid #e4ebf2;
        }
        h1 {
            text-align: center;
            color: #111;
            margin: 0 0 1.5rem;
        }
        h2 {
            margin: 0 0 0.75rem;
            font-size: 1.15rem;
            color: #1f2937;
        }
        .form-group {
            margin-bottom: 1.5rem;
        }
        label {
            display: block;
            margin-bottom: 0.5rem;
            font-weight: 600;
        }
        input[type="text"] {
            width: 100%;
            padding: 0.75rem;
            border: 1px solid #ccc;
            border-radius: 8px;
            box-sizing: border-box;
            font-size: 1rem;
        }
        button {
            padding: 1rem;
            font-size: 1rem;
            font-weight: 700;
            border: none;
            border-radius: 8px;
            cursor: pointer;
            transition: all 0.2s ease;
        }
        button:active {
            transform: scale(0.98);
        }
        .btn-start { background-color: #28a745; color: white; }
        .btn-start:hover { background-color: #218838; }
        .btn-stop { background-color: #dc3545; color: white; }
        .btn-stop:hover { background-color: #c82333; }
        .btn-mark { background-color: #ffc107; color: #212529; }
        .btn-mark:hover { background-color: #e0a800; }
        .btn-download {
            background-color: #007bff;
            color: white;
            grid-column: 1 / -1;
            margin-top: 1rem;
        }
        .btn-download:hover { background-color: #0069d9; }
        .btn-delete {
            background-color: #6c757d;
            color: white;
        }
        .btn-delete:hover { background-color: #5a6268; }
        .mark-form {
            margin-top: 1.5rem;
            margin-bottom: 1.5rem;
        }
        .mark-form button {
            width: 100%;
        }
        .button-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
            margin-top: 1.5rem;
        }
        .flash {
            padding: 1rem;
            margin-bottom: 1.5rem;
            border-radius: 8px;
            text-align: center;
            font-weight: 600;
        }
        .flash-success { background-color: #d4edda; color: #155724; }
        .flash-info { background-color: #d1ecf1; color: #0c5460; }
        .status-section {
            margin-bottom: 1.75rem;
        }
        .status-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
            gap: 1rem;
        }
        .status-card {
            background: #f9fbfc;
            border-radius: 12px;
            border: 1px solid #e3e9ef;
            padding: 1rem 1.25rem;
            box-shadow: 0 10px 24px rgba(15, 23, 42, 0.08);
        }
        .status-card-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 0.75rem;
            margin-bottom: 0.5rem;
        }
        .status-indicator {
            width: 12px;
            height: 12px;
            border-radius: 999px;
            display: inline-block;
            box-shadow: 0 0 0 2px rgba(255, 255, 255, 0.8);
        }
        .status-indicator.status-online { background-color: #28a745; }
        .status-indicator.status-recording { background-color: #dc3545; }
        .status-indicator.status-warning { background-color: #ffc107; }
        .status-indicator.status-offline { background-color: #6c757d; }
        .status-ip {
            font-weight: 700;
            color: #0f172a;
            flex: 1;
        }
        .status-state {
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.05em;
            color: #475569;
        }
        .status-card-body .status-line {
            margin-bottom: 0.35rem;
            font-size: 0.9rem;
            color: #1e293b;
        }
        .status-card-body .status-line.small {
            font-size: 0.8rem;
            color: #64748b;
        }
        .status-card-body .status-line.error {
            color: #b91c1c;
            font-weight: 600;
        }
        .status-empty {
            font-size: 0.9rem;
            color: #475569;
            padding: 0.85rem 1rem;
            background: #f1f5f9;
            border-radius: 10px;
            border: 1px dashed #cbd5f5;
            text-align: center;
        }
        .download-status {
            display: flex;
            align-items: center;
            gap: 0.75rem;
            flex-wrap: wrap;
            background: #f9fbfc;
            border-radius: 12px;
            border: 1px solid #e3e9ef;
            padding: 0.85rem 1.1rem;
        }
        .status-pill {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            padding: 0.35rem 0.85rem;
            border-radius: 999px;
            font-weight: 700;
            font-size: 0.75rem;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            background: #cbd5f5;
            color: #0f172a;
        }
        .status-pill.status-running { background: #007bff; color: #fff; }
        .status-pill.status-success { background: #28a745; color: #fff; }
        .status-pill.status-warning { background: #ffc107; color: #212529; }
        .status-pill.status-error { background: #dc3545; color: #fff; }
        .status-pill.status-idle { background: #6c757d; color: #fff; }
        .download-message {
            font-size: 0.9rem;
            color: #1e293b;
        }
        .log-window {
            background-color: #1b1d21;
            color: #e5e7eb;
            padding: 1rem;
            border-radius: 12px;
            border: 1px solid #2f3136;
            max-height: 220px;
            overflow-y: auto;
            font-family: "SFMono-Regular", Menlo, Monaco, Consolas, "Liberation Mono", "Courier New", monospace;
            font-size: 0.85rem;
            margin: 0;
        }
    </style>
</head>
<body>
    <div class="container">
        <h1>Pi Multi-Cam Controller</h1>

        {% with messages = get_flashed_messages(with_categories=true) %}
          {% if messages %}
            {% for category, message in messages %}
              <div class="flash flash-{{ category }}">{{ message }}</div>
            {% endfor %}
          {% endif %}
        {% endwith %}

        <div class="status-section">
            <h2>Node Status</h2>
            <div id="node-status-grid" class="status-grid">
                <div class="status-empty">Waiting for heartbeat...</div>
            </div>
        </div>

        <div class="status-section">
            <h2>Download Status</h2>
            <div id="download-status" class="download-status">
                <span class="status-pill status-idle">IDLE</span>
                <span class="download-message">No downloads in progress.</span>
            </div>
        </div>

        <div class="status-section">
            <h2>Activity Log</h2>
            <pre id="log-window" class="log-window" aria-live="polite">Waiting for log output…</pre>
        </div>

        <form action="{{ url_for('start') }}" method="POST">
            <div class="form-group">
                <label for="take_name">Take Name</label>
                <input type="text" id="take_name" name="take_name" placeholder="e.g., test1_no_wind" required>
            </div>
            <button type="submit" class="btn-start">START</button>
        </form>

        <form action="{{ url_for('mark') }}" method="POST" class="mark-form">
            <div class="form-group">
                <label for="mark_note">Marker Note (optional)</label>
                <input type="text" id="mark_note" name="mark_note" placeholder="Describe the marker (optional)">
            </div>
            <button type="submit" class="btn-mark">MARK</button>
        </form>

        <div class="button-grid">
            <form action="{{ url_for('stop') }}" method="POST" style="margin:0;">
                <button type="submit" class="btn-stop" style="width:100%;">STOP</button>
            </form>
            <form action="{{ url_for('download') }}" method="POST" style="margin:0;">
                <button type="submit" class="btn-download">DOWNLOAD & SORT FILES</button>
            </form>
            <form action="{{ url_for('wipe') }}" method="POST" style="margin:0;" onsubmit="return confirm('Delete all videos from every node? This cannot be undone.');">
                <button type="submit" class="btn-delete" style="width:100%;">DELETE OLD RECORDINGS</button>
            </form>
        </div>
    </div>
    <script>
        const nodeStatusGrid = document.getElementById('node-status-grid');
        const downloadStatusEl = document.getElementById('download-status');
        const logWindow = document.getElementById('log-window');

        const stateLabels = {
            online: 'Online',
            recording: 'Recording',
            offline: 'Offline',
            warning: 'Online (Check logs)'
        };

        function escapeHtml(value) {
            if (value === null || value === undefined) {
                return '';
            }
            return String(value)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function formatRelativeTime(isoString) {
            if (!isoString) {
                return '—';
            }
            const parsed = Date.parse(isoString);
            if (Number.isNaN(parsed)) {
                return isoString;
            }
            const diffMs = Date.now() - parsed;
            if (diffMs < 0) {
                return 'just now';
            }
            const diffSec = Math.floor(diffMs / 1000);
            if (diffSec < 5) return 'just now';
            if (diffSec < 60) return `${diffSec}s ago`;
            const diffMin = Math.floor(diffSec / 60);
            if (diffMin < 60) return `${diffMin}m ago`;
            const diffHr = Math.floor(diffMin / 60);
            if (diffHr < 24) return `${diffHr}h ago`;
            const diffDay = Math.floor(diffHr / 24);
            return `${diffDay}d ago`;
        }

        function renderNodeStatuses(nodes) {
            if (!nodeStatusGrid) {
                return;
            }
            nodeStatusGrid.innerHTML = '';
            const entries = Object.values(nodes || {});
            if (!entries.length) {
                const empty = document.createElement('div');
                empty.className = 'status-empty';
                empty.textContent = 'No nodes configured.';
                nodeStatusGrid.appendChild(empty);
                return;
            }

            entries.forEach((node) => {
                const card = document.createElement('div');
                card.className = 'status-card';

                let stateClass = 'offline';
                if (node.reachable) {
                    if (node.recording) {
                        stateClass = 'recording';
                    } else if (node.last_error) {
                        stateClass = 'warning';
                    } else {
                        stateClass = 'online';
                    }
                }

                const takeDisplay = node.recording ? (node.take_name || '—') : '—';
                const heartbeatDisplay = formatRelativeTime(node.last_heartbeat);
                const errorBlock = node.last_error
                    ? `<div class="status-line error">⚠️ ${escapeHtml(node.last_error)}</div>`
                    : '';

                card.innerHTML = `
                    <div class="status-card-header">
                        <span class="status-indicator status-${stateClass}" title="${escapeHtml(stateLabels[stateClass] || stateClass)}"></span>
                        <span class="status-ip">${escapeHtml(node.ip || 'Unknown')}</span>
                        <span class="status-state">${escapeHtml(stateLabels[stateClass] || stateClass)}</span>
                    </div>
                    <div class="status-card-body">
                        <div class="status-line"><strong>Recording:</strong> ${node.recording ? 'Yes' : 'No'}</div>
                        <div class="status-line"><strong>Take:</strong> ${escapeHtml(takeDisplay)}</div>
                        <div class="status-line"><strong>Heartbeat:</strong> ${escapeHtml(heartbeatDisplay)}</div>
                        <div class="status-line small"><strong>Last Cmd:</strong> ${escapeHtml(node.last_command || '—')}</div>
                        ${errorBlock}
                    </div>
                `;

                nodeStatusGrid.appendChild(card);
            });
        }

        function renderDownloadStatus(download) {
            if (!downloadStatusEl) {
                return;
            }
            const state = (download && download.state) ? String(download.state).toLowerCase() : 'idle';
            const safeState = state.replace(/[^a-z0-9_-]/g, '') || 'idle';
            const message = download && download.message ? download.message : 'No downloads in progress.';
            downloadStatusEl.innerHTML = `
                <span class="status-pill status-${safeState}">${escapeHtml(safeState.toUpperCase())}</span>
                <span class="download-message">${escapeHtml(message)}</span>
            `;
            if (download && download.last_updated) {
                downloadStatusEl.title = `Last updated ${download.last_updated}`;
            }
        }

        function renderLogs(logEntries) {
            if (!logWindow) {
                return;
            }
            if (!logEntries || !logEntries.length) {
                logWindow.textContent = 'No log entries yet.';
                return;
            }
            const nearBottom = (logWindow.scrollTop + logWindow.clientHeight + 40) >= logWindow.scrollHeight;
            const lines = logEntries.map((entry) => {
                const timestamp = entry.timestamp ? `[${entry.timestamp}] ` : '';
                return `${timestamp}${entry.message}`;
            });
            logWindow.textContent = lines.join('\n');
            if (nearBottom) {
                logWindow.scrollTop = logWindow.scrollHeight;
            }
        }

        async function refreshStatus() {
            try {
                const response = await fetch('/api/status', { cache: 'no-store' });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const data = await response.json();
                renderNodeStatuses(data.nodes);
                renderDownloadStatus(data.download);
            } catch (error) {
                console.warn('Failed to refresh status', error);
            }
        }

        async function refreshLogs() {
            try {
                const response = await fetch('/api/logs?limit=250', { cache: 'no-store' });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const data = await response.json();
                renderLogs(data.logs);
            } catch (error) {
                console.warn('Failed to refresh logs', error);
            }
        }

        document.addEventListener('DOMContentLoaded', () => {
            refreshStatus();
            refreshLogs();
            setInterval(refreshStatus, 5000);
            setInterval(refreshLogs, 4000);
        });
    </script>
</body>
</html>
"""


@app.before_first_request
def _initialize_background_services():
    ensure_status_monitor_running()


@app.route('/')
def index():
    """Serves the main HTML page."""
    return render_template_string(HTML_TEMPLATE)


@app.route('/api/status')
def api_status():
    """Return the latest status for each node and download activity."""
    return jsonify({
        "nodes": get_node_status_snapshot(),
        "download": get_download_status_snapshot(),
        "server_time": datetime.now().isoformat(),
    })


@app.route('/api/logs')
def api_logs():
    """Return a rolling window of captured log lines."""
    limit = request.args.get('limit', default=250, type=int)
    return jsonify({
        "logs": get_logs_snapshot(limit)
    })

@app.route('/start', methods=['POST'])
def start():
    """Handles the START command."""
    take_name = request.form.get('take_name')
    if not take_name:
        take_name = f'take_{datetime.now().strftime("%Y%m%d_%H%M%S")}'
    
    # Sanitize take_name: remove spaces, keep only safe chars
    safe_take_name = "".join(c for c in take_name if c.isalnum() or c in ('_','-')).rstrip()
    if not safe_take_name: # Handle case where name is only symbols
        safe_take_name = f'take_{datetime.now().strftime("%Y%m%d_%H%M%S")}'

    command = f"START:{safe_take_name}"
    asyncio.run(broadcast_command(command))
    flash(f"Started recording for take: {safe_take_name}", "success")
    return redirect(url_for('index'))

@app.route('/stop', methods=['POST'])
def stop():
    """Handles the STOP command."""
    asyncio.run(broadcast_command('STOP'))
    flash("Stopped recording.", "success")
    return redirect(url_for('index'))

@app.route('/mark', methods=['POST'])
def mark():
    """Handles the MARK command."""
    raw_note = request.form.get('mark_note', '')
    note = raw_note.replace('\r', ' ').replace('\n', ' ').strip()

    if note and len(note) > 200:
        # Trim overly long notes to fit comfortably in logs/commands
        note = note[:200].rstrip()

    if note:
        command = f"MARK:{note}"
        flash_message = f"Marker added: {note}"
    else:
        command = 'MARK'
        flash_message = "Marker added."

    asyncio.run(broadcast_command(command))
    flash(flash_message, "info")
    return redirect(url_for('index'))

@app.route('/download', methods=['POST'])
def download():
    """Handles the DOWNLOAD command by starting a background thread."""
    print(f"[{datetime.now().isoformat()}] [Info] Download request received. Starting background thread.")
    # Run the download in a separate thread so it doesn't block the web server
    threading.Thread(target=download_files_threaded, daemon=True).start()
    flash("Download & Sort started in background. Files will be organized by take name.", "info")
    return redirect(url_for('index'))


@app.route('/wipe', methods=['POST'])
def wipe():
    """Deletes recordings from all nodes."""
    remote_command = f"rm -f {REMOTE_VIDEO_PATH}*.mp4 {REMOTE_VIDEO_PATH}*.txt"
    results = asyncio.run(broadcast_shell_command(remote_command))

    failures = []
    for ip, result in zip(PI_NODES, results):
        if isinstance(result, Exception):
            failures.append(f"{ip}: {result}")
        elif result != 0:
            failures.append(f"{ip}: exit code {result}")

    if failures:
        fail_message = "; ".join(failures)
        flash(f"Deletion completed with errors: {fail_message}", "info")
    else:
        flash("Old recordings deleted from all nodes.", "success")

    return redirect(url_for('index'))

if __name__ == "__main__":
    print("--- Starting Pi Multi-Cam Controller Web App ---")
    print(f"--- Access at: http://<your_control_pi_ip>:8080 ---")
    app.run(host='0.0.0.0', port=8080)
