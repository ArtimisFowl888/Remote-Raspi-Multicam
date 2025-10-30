import asyncio
import os
import subprocess
import threading
import re
import shutil
from datetime import datetime
from flask import Flask, render_template_string, request, redirect, url_for, flash

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

# --- Asynchronous Command Functions ---

async def send_command(ip, command):
    """Asynchronously sends a command to a single Pi node."""
    try:
        print(f"[{datetime.now().isoformat()}] Sending {command} to {ip}...")
        reader, writer = await asyncio.wait_for(
            asyncio.open_connection(ip, PORT),
            timeout=5.0
        )
        
        writer.write(command.encode('utf-8'))
        await writer.drain()
        
        response = await asyncio.wait_for(reader.read(100), timeout=2.0)
        print(f"[{datetime.now().isoformat()}] [Response from {ip}]: {response.decode('utf-8')}")
        
        writer.close()
        await writer.wait_closed()
        
    except asyncio.TimeoutError:
        print(f"[{datetime.now().isoformat()}] [Error] Timeout connecting or communicating with {ip}.")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Failed to send command to {ip}: {e}")

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
    print(f"\n[{datetime.now().isoformat()}] --- Starting file download thread ---")
    os.makedirs(DOWNLOAD_PATH_BASE, exist_ok=True)
    
    # --- Step 1: Download all files from all nodes ---
    print(f"[{datetime.now().isoformat()}] --- [Step 1] Syncing files via rsync ---")
    synced_ips = []
    for ip in PI_NODES:
        # We download into a temporary folder named after the IP
        # This folder will be sorted from and then deleted
        temp_target_dir = os.path.join(DOWNLOAD_PATH_BASE, f"__temp_{ip}")
        os.makedirs(temp_target_dir, exist_ok=True)
            
        print(f"\n[{datetime.now().isoformat()}] [Info] Downloading files from {ip} to {temp_target_dir}...")
        
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
        except FileNotFoundError:
            print(f"[{datetime.now().isoformat()}] [Error] rsync command not found. Is it installed?")
    
    # --- Step 2: Sort downloaded files into take_name folders ---
    print(f"\n[{datetime.now().isoformat()}] --- [Step 2] Sorting downloaded files by take_name ---")
    for ip in synced_ips:
        source_dir = os.path.join(DOWNLOAD_PATH_BASE, f"__temp_{ip}")
        safe_ip = ip.replace('.', '_')
        
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
                else:
                    print(f"[{datetime.now().isoformat()}] [Warning] File '{filename}' in {source_dir} did not match expected format. Skipping.")

        except Exception as e:
            print(f"[{datetime.now().isoformat()}] [Error] Failed to sort files for {ip}: {e}")

        # --- Step 3: Clean up empty source IP directories ---
        try:
            if os.path.exists(source_dir) and not os.listdir(source_dir):
                os.rmdir(source_dir)
                print(f"[{datetime.now().isoformat()}] Cleaned up empty directory: {source_dir}")
        except Exception as e:
            print(f"[{datetime.now().isoformat()}] [Error] Could not remove empty directory {source_dir}: {e}")

    print(f"[{datetime.now().isoformat()}] --- File download and sorting thread finished ---")


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
            padding: 2rem;
            background-color: #f4f7f6;
            color: #333;
            display: grid;
            place-items: center;
            min-height: 90vh;
        }
        .container {
            width: 100%;
            max-width: 500px;
            background: #ffffff;
            border-radius: 12px;
            box-shadow: 0 10px 30px rgba(0, 0, 0, 0.07);
            padding: 2.5rem;
            border: 1px solid #e0e0e0;
        }
        h1 {
            text-align: center;
            color: #111;
            margin-top: 0;
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
            box-sizing: border-box; /* Important */
            font-size: 1rem;
        }
        .button-grid {
            display: grid;
            grid-template-columns: 1fr 1fr;
            gap: 1rem;
            margin-top: 1.5rem;
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
            grid-column: 1 / -1; /* Span full width */
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

        /* Flash messages */
        .flash {
            padding: 1rem;
            margin-bottom: 1.5rem;
            border-radius: 8px;
            text-align: center;
            font-weight: 600;
        }
        .flash-success { background-color: #d4edda; color: #155724; }
        .flash-info { background-color: #d1ecf1; color: #0c5460; }
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
</body>
</html>
"""

# --- Flask Routes ---

@app.route('/')
def index():
    """Serves the main HTML page."""
    return render_template_string(HTML_TEMPLATE)

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
    threading.Thread(target=download_files_threaded).start()
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
