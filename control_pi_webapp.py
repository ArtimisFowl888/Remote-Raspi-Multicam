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
import tempfile
from collections import deque
from dataclasses import dataclass, asdict
from datetime import datetime
from typing import Dict, Optional, Tuple
from flask import Flask, render_template_string, request, redirect, url_for, flash, jsonify, send_file, after_this_request

# --- Configuration ---
# !! IMPORTANT: Update this list with your Pi Nodes' static IP addresses !!
PI_NODES = [
    '192.168.1.101',
    '192.168.1.104',
    '192.168.1.103',
]
PORT = 9090  # Port specified in pi_node_listener.py
DOWNLOAD_PATH_BASE = os.path.expanduser("~/video_downloads") # Local folder to save files
REMOTE_VIDEO_PATH = "/home/pi/videos/"  # Path on the Pi Nodes
REMOTE_PI_USER = "pi"  # Username on the Pi Nodes
NODE_PREVIEW_PORT = 8081  # Preview HTTP server running on each node
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
    cpu_temp_c: Optional[float] = None

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
            "cpu_temp_c": self.cpu_temp_c,
        }


node_status_lock = threading.Lock()
node_statuses: Dict[str, NodeStatus] = {ip: NodeStatus(ip=ip) for ip in PI_NODES}
last_logged_node_states: Dict[str, Tuple[bool, bool, str, str, str, bool]] = {}
STATUS_POLL_INTERVAL_SECONDS = 10
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


def log_node_status_summary(force: bool = False) -> None:
    """Print a concise status summary for all nodes to the CLI when changes occur."""
    with node_status_lock:
        snapshot = {ip: status for ip, status in node_statuses.items()}

    if not snapshot:
        return

    current_time = time.time()
    current_ips = set(snapshot.keys())
    changed = force

    for ip, status in snapshot.items():
        state_key = (
            status.reachable,
            status.recording,
            status.take_name,
            status.last_error,
            status.last_command,
            bool(status.last_heartbeat),
        )
        if last_logged_node_states.get(ip) != state_key:
            changed = True
            last_logged_node_states[ip] = state_key

    removed = set(last_logged_node_states.keys()) - current_ips
    if removed:
        changed = True
        for ip in removed:
            del last_logged_node_states[ip]

    if not changed:
        return

    now_iso = datetime.now().isoformat()
    lines = []

    for ip in sorted(snapshot.keys()):
        status = snapshot[ip]
        if status.recording:
            state_label = "RECORDING"
        elif status.reachable:
            state_label = "ONLINE"
        else:
            state_label = "OFFLINE"

        take_display = status.take_name or "--"
        heartbeat_display = "--"
        if status.last_heartbeat:
            age = max(current_time - status.last_heartbeat, 0.0)
            if age < 5:
                heartbeat_display = "just now"
            elif age < 60:
                heartbeat_display = f"{int(age)}s ago"
            elif age < 3600:
                heartbeat_display = f"{int(age // 60)}m ago"
            else:
                heartbeat_display = f"{int(age // 3600)}h ago"

        error_display = status.last_error or "none"
        last_cmd_display = status.last_command or "--"
        lines.append(
            f"{ip} [{state_label}] take={take_display} heartbeat={heartbeat_display} cmd={last_cmd_display} error={error_display}"
        )

    print(f"[{now_iso}] [Status] Node summary:\n  " + "\n  ".join(lines))


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
            log_node_status_summary()
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


def compute_directory_size(path: str) -> int:
    total = 0
    for root, _, files in os.walk(path):
        for fname in files:
            fpath = os.path.join(root, fname)
            try:
                total += os.path.getsize(fpath)
            except OSError:
                continue
    return total


def human_readable_size(num_bytes: int) -> str:
    if num_bytes <= 0:
        return "0 B"
    units = ['B', 'KB', 'MB', 'GB', 'TB']
    value = float(num_bytes)
    for unit in units:
        if value < 1024 or unit == units[-1]:
            return f"{value:.2f} {unit}"
        value /= 1024


def list_take_directories() -> list:
    """Enumerate all take directories sorted by most recent."""
    takes = []
    if not os.path.isdir(DOWNLOAD_PATH_BASE):
        return takes

    base_path = os.path.abspath(DOWNLOAD_PATH_BASE)
    try:
        entries = os.listdir(base_path)
    except OSError:
        return takes

    for entry in entries:
        full_path = os.path.join(base_path, entry)
        if not os.path.isdir(full_path):
            continue
        try:
            mtime = os.path.getmtime(full_path)
        except OSError:
            continue
        size_bytes = compute_directory_size(full_path)
        takes.append({
            "name": entry,
            "path": full_path,
            "modified_epoch": mtime,
            "modified_display": datetime.fromtimestamp(mtime).strftime("%Y-%m-%d %H:%M:%S"),
            "size_display": human_readable_size(size_bytes),
            "size_bytes": size_bytes,
        })

    takes.sort(key=lambda item: item["modified_epoch"], reverse=True)
    return takes


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
            cpu_temp_value = status_data.get('cpu_temp_c')
            cpu_temp_c = None
            if cpu_temp_value is not None:
                try:
                    cpu_temp_c = float(cpu_temp_value)
                except (TypeError, ValueError):
                    cpu_temp_c = None
            update_node_status(
                ip,
                recording=bool(status_data.get('recording', False)),
                take_name=status_data.get('take_name') or "",
                last_error=status_data.get('last_error', ''),
                last_response=response_text,
                last_heartbeat=heartbeat_time,
                cpu_temp_c=cpu_temp_c,
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
        :root {
            color-scheme: light;
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif;
            --bg: #f2f4f7;
            --card-bg: #fff;
            --card-border: #dde4f0;
            --accent: #2563eb;
            --accent-dark: #1d4ed8;
            --success: #16a34a;
            --danger: #dc2626;
            --warning: #f97316;
            --muted: #475569;
        }
        * {
            box-sizing: border-box;
        }
        body {
            margin: 0;
            padding: 2rem 1rem 3rem;
            background: var(--bg);
            color: #111827;
            display: flex;
            justify-content: center;
        }
        .container {
            width: 100%;
            max-width: 960px;
            background: var(--card-bg);
            border-radius: 18px;
            border: 1px solid var(--card-border);
            box-shadow: 0 18px 40px rgba(15, 23, 42, 0.08);
            padding: 2.5rem 2.5rem 3rem;
        }
        h1 {
            margin: 0 0 2rem;
            text-align: center;
            font-size: 2rem;
        }
        h2 {
            margin: 0;
            font-size: 1.15rem;
            color: #0f172a;
        }
        .flash {
            padding: 0.9rem 1.1rem;
            border-radius: 10px;
            margin-bottom: 1rem;
            font-weight: 600;
        }
        .flash-success { background: #d1fae5; color: #065f46; }
        .flash-info { background: #dbeafe; color: #1e3a8a; }
        .flash-warning { background: #fef3c7; color: #92400e; }
        .flash-error { background: #fee2e2; color: #b91c1c; }
        .grid-two {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 1.25rem;
            margin-bottom: 1.5rem;
        }
        .card {
            border: 1px solid var(--card-border);
            border-radius: 16px;
            padding: 1.5rem;
            background: var(--card-bg);
            box-shadow: inset 0 0 0 0 rgba(0,0,0,0);
        }
        .card + .card {
            margin-top: 0;
        }
        .card-header {
            display: flex;
            justify-content: space-between;
            align-items: center;
            margin-bottom: 1rem;
        }
        .card-header p {
            margin: 0;
            color: var(--muted);
            font-size: 0.9rem;
        }
        label {
            display: block;
            font-size: 0.9rem;
            font-weight: 600;
            margin-bottom: 0.35rem;
        }
        input[type="text"] {
            width: 100%;
            border: 1px solid #cbd5f5;
            border-radius: 10px;
            padding: 0.75rem 0.9rem;
            font-size: 1rem;
        }
        .form-control {
            margin-bottom: 1rem;
        }
        .btn {
            display: inline-flex;
            justify-content: center;
            align-items: center;
            width: 100%;
            border-radius: 10px;
            font-weight: 600;
            font-size: 0.95rem;
            border: none;
            cursor: pointer;
            padding: 0.8rem 1rem;
            transition: background 0.2s ease, transform 0.1s ease;
            text-decoration: none;
            color: #fff;
        }
        .btn:active {
            transform: scale(0.98);
        }
        .btn-accent { background: var(--accent); }
        .btn-accent:hover { background: var(--accent-dark); }
        .btn-success { background: var(--success); }
        .btn-success:hover { background: #15803d; }
        .btn-warning { background: var(--warning); color: #fff; }
        .btn-warning:hover { background: #ea580c; }
        .btn-danger { background: var(--danger); }
        .btn-danger:hover { background: #b91c1c; }
        .btn-ghost {
            border: 1px solid #cbd5f5;
            background: transparent;
            color: #0f172a;
        }
        .btn-ghost:hover {
            background: #eef2ff;
        }
        .actions-list {
            display: flex;
            flex-direction: column;
            gap: 0.75rem;
        }
        .node-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 1rem;
        }
        .node-table th,
        .node-table td {
            padding: 0.65rem 0.75rem;
            border-bottom: 1px solid #eceff7;
            text-align: left;
        }
        .node-table th {
            font-size: 0.8rem;
            text-transform: uppercase;
            letter-spacing: 0.08em;
            color: var(--muted);
        }
        .node-table tbody td {
            font-size: 0.9rem;
        }
        .node-table .status-pill {
            font-size: 0.7rem;
            border-radius: 999px;
            padding: 0.2rem 0.6rem;
        }
        .status-pill {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            font-weight: 600;
            letter-spacing: 0.06em;
            text-transform: uppercase;
            padding: 0.35rem 0.75rem;
            border-radius: 999px;
        }
        .status-pill.status-running { background: #dbeafe; color: #1d4ed8; }
        .status-pill.status-success { background: #dcfce7; color: #15803d; }
        .status-pill.status-warning { background: #fef3c7; color: #92400e; }
        .status-pill.status-error { background: #fee2e2; color: #b91c1c; }
        .status-pill.status-idle { background: #e2e8f0; color: #334155; }
        .status-pill.status-online { background: #dcfce7; color: #166534; }
        .status-pill.status-recording { background: #fee2e2; color: #b91c1c; }
        .status-pill.status-offline { background: #e2e8f0; color: #475569; }
        .download-status {
            display: flex;
            align-items: center;
            gap: 0.75rem;
        }
        .download-message {
            font-size: 0.95rem;
            color: #0f172a;
        }
        .link-list {
            list-style: none;
            padding: 0;
            margin: 1rem 0 0;
            display: flex;
            flex-direction: column;
            gap: 0.65rem;
        }
        .link-list a {
            text-decoration: none;
            color: var(--accent);
            font-weight: 600;
        }
        .link-list a:hover {
            color: var(--accent-dark);
            text-decoration: underline;
        }
        .section-title {
            margin-bottom: 0.35rem;
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

        <div class="grid-two">
            <div class="card">
                <div class="card-header">
                    <h2>Take Controls</h2>
                    <p>Start new takes and drop markers.</p>
                </div>
                <form action="{{ url_for('start') }}" method="POST">
                    <div class="form-control">
                        <label for="take_name">Take Name</label>
                        <input type="text" id="take_name" name="take_name" placeholder="e.g., test1_no_wind" required>
                    </div>
                    <button type="submit" class="btn btn-success">Start Recording</button>
                </form>
                <form action="{{ url_for('stop') }}" method="POST" style="margin-top:0.75rem;">
                    <button type="submit" class="btn btn-danger">Stop Recording</button>
                </form>
                <form action="{{ url_for('mark') }}" method="POST" style="margin-top:1rem;">
                    <div class="form-control">
                        <label for="mark_note">Marker Note (optional)</label>
                        <input type="text" id="mark_note" name="mark_note" placeholder="Describe the marker (optional)">
                    </div>
                    <button type="submit" class="btn btn-warning">Add Marker</button>
                </form>
            </div>

            <div class="card">
                <div class="card-header">
                    <h2>System Actions</h2>
                    <p>Control all camera nodes at once.</p>
                </div>
                <div class="actions-list">
                    <form action="{{ url_for('download') }}" method="POST">
                        <button type="submit" class="btn btn-accent">Download & Sort Files</button>
                    </form>
                    <form action="{{ url_for('wipe') }}" method="POST" onsubmit="return confirm('Delete all videos from every node? This cannot be undone.');">
                        <button type="submit" class="btn btn-ghost">Delete Old Recordings</button>
                    </form>
                    <a href="{{ url_for('takes_index') }}" class="btn btn-ghost">Manage Downloaded Takes</a>
                </div>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <h2>Node Status</h2>
                <p>Live snapshot of each Pi node.</p>
            </div>
            <div class="node-summary">
                <table class="node-table" aria-describedby="node-status-table-body">
                    <thead>
                        <tr>
                            <th scope="col">Node</th>
                            <th scope="col">Status</th>
                            <th scope="col">Recording</th>
                            <th scope="col">Take</th>
                            <th scope="col">Heartbeat</th>
                            <th scope="col">CPU Temp</th>
                            <th scope="col">Last Cmd</th>
                            <th scope="col">Last Error</th>
                        </tr>
                    </thead>
                    <tbody id="node-status-table-body">
                        {% if initial_nodes %}
                            {% for ip, node in initial_nodes|dictsort %}
                                {% set recording = node.recording %}
                                {% set reachable = node.reachable %}
                                {% set has_error = node.last_error %}
                                {% if recording %}
                                    {% set state_class = 'recording' %}
                                    {% set state_label = 'Recording' %}
                                {% elif reachable %}
                                    {% if has_error %}
                                        {% set state_class = 'warning' %}
                                        {% set state_label = 'Online (Check logs)' %}
                                    {% else %}
                                        {% set state_class = 'online' %}
                                        {% set state_label = 'Online' %}
                                    {% endif %}
                                {% else %}
                                    {% set state_class = 'offline' %}
                                    {% set state_label = 'Offline' %}
                                {% endif %}
                                <tr>
                                    <td>{{ ip }}</td>
                                    <td><span class="status-pill status-{{ state_class }}">{{ state_label }}</span></td>
                                    <td>{{ 'Yes' if recording else 'No' }}</td>
                                    <td>{{ node.take_name or '--' }}</td>
                                    <td>{{ node.last_heartbeat or '--' }}</td>
                                    <td>{{ ('%.1f C' % node.cpu_temp_c) if node.cpu_temp_c is not none else '--' }}</td>
                                    <td>{{ node.last_command or '--' }}</td>
                                    <td>{{ node.last_error or '--' }}</td>
                                </tr>
                            {% endfor %}
                        {% else %}
                            <tr class="empty-row">
                                <td colspan="8">Waiting for heartbeat...</td>
                            </tr>
                        {% endif %}
                    </tbody>
                </table>
            </div>
        </div>

        <div class="card">
            <div class="card-header">
                <h2>Live View Links</h2>
                <p>Open each node’s low-res preview.</p>
            </div>
            {% if preview_nodes %}
                <ul class="link-list">
                    {% for ip in preview_nodes %}
                        <li>
                            <a href="http://{{ ip }}:{{ preview_port }}/preview.mjpg" target="_blank" rel="noopener noreferrer">
                                {{ ip }} – Open Live View
                            </a>
                        </li>
                    {% endfor %}
                </ul>
            {% else %}
                <p class="empty-row">No nodes configured. Update PI_NODES to enable preview links.</p>
            {% endif %}
        </div>

        <div class="card">
            <div class="card-header">
                <h2>Download Status</h2>
                <p>Background progress from the Control Pi.</p>
            </div>
            <div id="download-status" class="download-status">
                <span class="status-pill status-idle">IDLE</span>
                <span class="download-message">No downloads in progress.</span>
            </div>
        </div>
    </div>
    <script>
        const initialData = {
            nodes: {{ initial_nodes | tojson }},
            download: {{ initial_download | tojson }},
            serverTime: {{ initial_server_time | tojson }}
        };

        const nodeStatusTableBody = document.getElementById('node-status-table-body');
        const downloadStatusEl = document.getElementById('download-status');

        let statusRequestInFlight = false;
        let downloadRefreshTimeoutId = null;

        const stateLabels = {
            online: 'Online',
            recording: 'Recording',
            offline: 'Offline',
            warning: 'Online (Check logs)'
        };

        function getNodeState(node) {
            if (node && node.reachable) {
                if (node.recording) {
                    return 'recording';
                }
                if (node.last_error) {
                    return 'warning';
                }
                return 'online';
            }
            return 'offline';
        }

        function formatCpuTempValue(node) {
            if (!node || node.cpu_temp_c === undefined || node.cpu_temp_c === null) {
                return '--';
            }
            const numericValue = Number(node.cpu_temp_c);
            if (!Number.isFinite(numericValue)) {
                return '--';
            }
            return `${numericValue.toFixed(1)} C`;
        }

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
                return '--';
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

        function renderNodeStatusTable(nodes) {
            if (!nodeStatusTableBody) {
                return;
            }
            nodeStatusTableBody.innerHTML = '';
            const entries = Object.values(nodes || {}).sort((a, b) => {
                const left = (a && a.ip) ? String(a.ip) : '';
                const right = (b && b.ip) ? String(b.ip) : '';
                return left.localeCompare(right);
            });
            if (!entries.length) {
                const emptyRow = document.createElement('tr');
                emptyRow.className = 'empty-row';
                emptyRow.innerHTML = '<td colspan="8">No nodes configured.</td>';
                nodeStatusTableBody.appendChild(emptyRow);
                return;
            }

            entries.forEach((node) => {
                const row = document.createElement('tr');
                const nodeState = getNodeState(node);
                const takeDisplay = node.take_name || '--';
                const recordingLabel = node.recording ? 'Yes' : 'No';
                const heartbeatDisplay = formatRelativeTime(node.last_heartbeat);
                const cpuTempDisplay = formatCpuTempValue(node);
                const lastCommand = node.last_command ? node.last_command : '--';
                const lastError = node.last_error ? node.last_error : '--';
                row.innerHTML = `
                    <td>${escapeHtml(node.ip || 'Unknown')}</td>
                    <td><span class="status-pill status-${nodeState}">${escapeHtml(stateLabels[nodeState] || nodeState)}</span></td>
                    <td>${escapeHtml(recordingLabel)}</td>
                    <td>${escapeHtml(takeDisplay)}</td>
                    <td>${escapeHtml(heartbeatDisplay)}</td>
                    <td>${escapeHtml(cpuTempDisplay)}</td>
                    <td>${escapeHtml(lastCommand)}</td>
                    <td>${escapeHtml(lastError)}</td>
                `;
                nodeStatusTableBody.appendChild(row);
            });
        }

        function scheduleDownloadRefresh(active) {
            if (downloadRefreshTimeoutId) {
                clearTimeout(downloadRefreshTimeoutId);
                downloadRefreshTimeoutId = null;
            }
            if (active) {
                downloadRefreshTimeoutId = setTimeout(() => {
                    refreshStatus();
                }, 2500);
            }
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
            } else {
                downloadStatusEl.removeAttribute('title');
            }
            const isActive = Boolean(download && download.in_progress);
            scheduleDownloadRefresh(isActive);
        }

        async function refreshStatus() {
            if (statusRequestInFlight) {
                return;
            }
            statusRequestInFlight = true;
            try {
                const response = await fetch('/api/status', { cache: 'no-store' });
                if (!response.ok) {
                    throw new Error(`HTTP ${response.status}`);
                }
                const data = await response.json();
                renderNodeStatusTable(data.nodes);
                renderDownloadStatus(data.download);
            } catch (error) {
                console.warn('Failed to refresh status', error);
            } finally {
                statusRequestInFlight = false;
            }
        }

        document.addEventListener('DOMContentLoaded', () => {
            renderNodeStatusTable(initialData.nodes);
            renderDownloadStatus(initialData.download);

            refreshStatus();
            setInterval(refreshStatus, 10000);
        });
    </script>
</body>
</html>
"""

TAKES_TEMPLATE = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Downloaded Takes</title>
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
            max-width: 900px;
            background: #ffffff;
            border-radius: 16px;
            box-shadow: 0 18px 45px rgba(14, 30, 37, 0.12);
            padding: 2.5rem 2.25rem 2.75rem;
            border: 1px solid #e4ebf2;
        }
        h1 {
            margin-top: 0;
            text-align: center;
        }
        .take-table {
            width: 100%;
            border-collapse: collapse;
            margin-top: 1.5rem;
        }
        .take-table th,
        .take-table td {
            padding: 0.75rem 0.9rem;
            border-bottom: 1px solid #e5e7eb;
            text-align: left;
        }
        .take-table th {
            text-transform: uppercase;
            font-size: 0.8rem;
            letter-spacing: 0.08em;
            color: #475569;
            background: #f8fafc;
        }
        .actions form {
            margin: 0;
        }
        .btn-primary {
            background-color: #007bff;
            color: white;
            padding: 0.65rem 1.1rem;
            border-radius: 8px;
            font-weight: 600;
            border: none;
            cursor: pointer;
        }
        .btn-primary:hover {
            background-color: #0062cc;
        }
        .header-actions {
            display: flex;
            justify-content: space-between;
            align-items: center;
            flex-wrap: wrap;
            gap: 1rem;
        }
        .back-link {
            color: #007bff;
            text-decoration: none;
            font-weight: 600;
        }
        .back-link:hover {
            text-decoration: underline;
        }
        .empty-state {
            text-align: center;
            padding: 2rem 1rem;
            color: #475569;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="header-actions">
            <h1>Downloaded Takes</h1>
            <a href="{{ url_for('index') }}" class="back-link">&larr; Back to Controller</a>
        </div>

        {% if takes %}
            <table class="take-table">
                <thead>
                    <tr>
                        <th>Take</th>
                        <th>Last Modified</th>
                        <th>Total Size</th>
                        <th>Actions</th>
                    </tr>
                </thead>
                <tbody>
                    {% for take in takes %}
                        <tr>
                            <td>{{ take.name }}</td>
                            <td>{{ take.modified_display }}</td>
                            <td>{{ take.size_display }}</td>
                            <td class="actions">
                                <form action="{{ url_for('download_take_archive', take_name=take.name) }}" method="POST">
                                    <button type="submit" class="btn-primary">Download ZIP</button>
                                </form>
                            </td>
                        </tr>
                    {% endfor %}
                </tbody>
            </table>
        {% else %}
            <div class="empty-state">
                <p>No takes have been downloaded yet.</p>
            </div>
        {% endif %}
    </div>
</body>
</html>
"""


@app.before_request
def _initialize_background_services():
    # Ensure the status monitor thread is running; the event makes this idempotent.
    ensure_status_monitor_running()


@app.route('/')
def index():
    """Serves the main HTML page with the latest cached state."""
    # Ensure background monitoring is running even if the app is reloaded.
    ensure_status_monitor_running()

    # Provide the most recent snapshots so the UI has immediate data
    # before the periodic polling kicks in.
    initial_nodes = get_node_status_snapshot()
    initial_download = get_download_status_snapshot()

    return render_template_string(
        HTML_TEMPLATE,
        initial_nodes=initial_nodes,
        initial_download=initial_download,
        initial_server_time=datetime.now().isoformat(),
        preview_nodes=PI_NODES,
        preview_port=NODE_PREVIEW_PORT,
    )



@app.route('/takes')
def takes_index():
    """List downloaded takes on the Control Pi."""
    takes = list_take_directories()
    return render_template_string(
        TAKES_TEMPLATE,
        takes=takes,
    )


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
    update_download_status('running', 'Preparing download & sort...', in_progress=True)
    # Run the download in a separate thread so it doesn't block the web server
    threading.Thread(target=download_files_threaded, daemon=True).start()
    flash("Download & Sort started in background. Files will be organized by take name.", "info")
    return redirect(url_for('index'))


@app.route('/takes/<path:take_name>/download', methods=['POST'])
def download_take_archive(take_name):
    """Bundle the specified take directory into a zip archive and send it to the user."""
    safe_name = take_name.strip()
    if not safe_name:
        flash("Invalid take name.", "error")
        return redirect(url_for('takes_index'))

    base_dir = os.path.abspath(DOWNLOAD_PATH_BASE)
    take_path = os.path.abspath(os.path.join(base_dir, safe_name))
    if not take_path.startswith(base_dir) or not os.path.isdir(take_path):
        flash("Take not found on controller.", "warning")
        return redirect(url_for('takes_index'))

    temp_dir = tempfile.mkdtemp(prefix="take_archive_")

    try:
        archive_base = os.path.join(temp_dir, safe_name)
        archive_path = shutil.make_archive(archive_base, 'zip', root_dir=take_path)
    except Exception as exc:
        shutil.rmtree(temp_dir, ignore_errors=True)
        flash(f"Failed to prepare archive: {exc}", "error")
        return redirect(url_for('takes_index'))

    filename = os.path.basename(archive_path)

    @after_this_request
    def cleanup(response):
        shutil.rmtree(temp_dir, ignore_errors=True)
        return response

    return send_file(
        archive_path,
        as_attachment=True,
        download_name=filename,
        mimetype='application/zip'
    )


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
