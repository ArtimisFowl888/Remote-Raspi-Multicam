import socket
import subprocess
import os
from datetime import datetime

# --- Configuration ---
HOST = '0.0.0.0'  # Listen on all available network interfaces
PORT = 9090       # The port to listen on
VIDEO_PATH = os.path.expanduser('~/videos') # Directory to save videos

# Set segment time in milliseconds. 1200000ms = 20 minutes.
# This results in files of approx. 3.75GB (at 25Mbps), safely under the 4GB limit.
SEGMENT_TIME_MS = '1200000'
# ---------------------

# Global variables to keep track of the recording state
# We use globals so the main loop can manage the state
# based on socket commands.
recording_process = None
log_file_handle = None

def get_pi_ip():
    """Gets the Pi's own IP address to use in filenames."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80)) # Connect to a known external IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Could not get IP: {e}. Defaulting to 'local-pi'.")
        return "local-pi"

def start_recording(take_name):
    """Starts the libcamera-vid recording process with a given take name."""
    global recording_process, log_file_handle
    
    if recording_process:
        print(f"[{datetime.now().isoformat()}] [Warning] Already recording. Ignoring START command.")
        return

    try:
        # Get identifying info for the filename
        pi_ip = get_pi_ip()
        safe_ip = pi_ip.replace('.', '_') # Make IP filesystem-safe
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        
        # Base filename format: <TakeName>_<SafeIP>_<Timestamp>
        base_filename = f"{take_name}_{safe_ip}_{timestamp}"
        
        # libcamera-vid will add the segment number (%06d)
        video_filename_template = os.path.join(VIDEO_PATH, f"{base_filename}_%06d.h264")
        log_filename = os.path.join(VIDEO_PATH, f"{base_filename}_markers.txt")

        # --- The Core libcamera-vid Command ---
        cmd = [
            'libcamera-vid',
            '-t', '0',               # -t 0: Record indefinitely
            '--timestamp',          # --timestamp: Embed NTP-synced system time. CRITICAL.
            '--codec', 'h264',      # --codec h264: Specify H.264 codec
            '--segment', SEGMENT_TIME_MS, # --segment: Split video into new files
            '-o', video_filename_template # -o: Output file template
        ]

        print(f"[{datetime.now().isoformat()}] [Info] Starting recording: {' '.join(cmd)}")
        # Start the recording process in the background
        recording_process = subprocess.Popen(cmd)
        
        # Open the marker log file (only ONE log file per take)
        log_file_handle = open(log_filename, 'w')
        log_file_handle.write(f"# Markers for {base_filename}\n")
        log_file_handle.write(f"# Recording started at: {datetime.now().isoformat()}\n")
        log_file_handle.write(f"# Video segmented every {SEGMENT_TIME_MS} ms.\n")
        log_file_handle.flush()
        
        print(f"[{datetime.now().isoformat()}] [Success] Recording started. PID: {recording_process.pid}")
        print(f"[{datetime.now().isoformat()}] [Info] Marker file created: {log_filename}")

    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Failed to start recording: {e}")
        recording_process = None
        log_file_handle = None

def stop_recording():
    """Stops the recording process gracefully."""
    global recording_process, log_file_handle
    
    if not recording_process:
        print(f"[{datetime.now().isoformat()}] [Warning] Not recording. Ignoring STOP command.")
        return

    try:
        print(f"[{datetime.now().isoformat()}] [Info] Stopping recording...")
        # Send SIGINT (Ctrl+C) to libcamera-vid for a clean stop
        recording_process.terminate() 
        recording_process.wait(timeout=5) # Wait for it to close
        print(f"[{datetime.now().isoformat()}] [Success] Recording stopped.")
    
    except subprocess.TimeoutExpired:
        print(f"[{datetime.now().isoformat()}] [Warning] Process did not terminate, killing.")
        recording_process.kill()
        
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Error stopping recording: {e}")

    finally:
        # Ensure state is always cleaned up
        recording_process = None
        if log_file_handle:
            try:
                log_file_handle.write(f"# Recording stopped at: {datetime.now().isoformat()}\n")
                log_file_handle.close()
                log_file_handle = None
                print(f"[{datetime.now().isoformat()}] [Info] Marker file closed.")
            except Exception as e:
                print(f"[{datetime.now().isoformat()}] [Error] Failed to close log file: {e}")

def mark_timecode():
    """Writes a marker (current timestamp) to the log file."""
    if not log_file_handle or log_file_handle.closed:
        print(f"[{datetime.now().isoformat()}] [Warning] Cannot mark timecode, not recording or log is closed.")
        return

    try:
        marker_time = datetime.now().isoformat()
        log_file_handle.write(f"MARK: {marker_time}\n")
        log_file_handle.flush() # Ensure it's written immediately
        print(f"[{datetime.now().isoformat()}] [Info] Marked timecode: {marker_time}")
        
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Failed to write marker: {e}")

def main():
    """Main function to run the socket server."""
    # Ensure video directory exists on startup
    try:
        os.makedirs(VIDEO_PATH, exist_ok=True)
        print(f"[{datetime.now().isoformat()}] [Info] Video storage directory: {VIDEO_PATH}")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Fatal] Could not create video directory: {e}. Exiting.")
        return

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((HOST, PORT))
            s.listen()
            print(f"[{datetime.now().isoformat()}] [Info] Pi Node Listener started on {HOST}:{PORT}")
            
            while True:
                conn, addr = s.accept()
                with conn:
                    print(f"[{datetime.now().isoformat()}] [Info] Connected by {addr}")
                    try:
                        data = conn.recv(1024)
                        if not data:
                            continue
                        
                        command = data.decode('utf-8').strip()
                        
                        if command.startswith('START:'):
                            parts = command.split(':', 1)
                            if len(parts) == 2 and parts[1]:
                                take_name = parts[1]
                                print(f"[{datetime.now().isoformat()}] [Info] Received START command with take name: {take_name}")
                                start_recording(take_name)
                                conn.sendall(b'ACK_START')
                            else:
                                print(f"[{datetime.now().isoformat()}] [Warning] START command received without take name.")
                                conn.sendall(b'ACK_BAD_START_FORMAT')
                        elif command == 'STOP':
                            print(f"[{datetime.now().isoformat()}] [Info] Received STOP command.")
                            stop_recording()
                            conn.sendall(b'ACK_STOP')
                        elif command == 'MARK':
                            print(f"[{datetime.now().isoformat()}] [Info] Received MARK command.")
                            mark_timecode()
                            conn.sendall(b'ACK_MARK')
                        else:
                            print(f"[{datetime.now().isoformat()}] [Warning] Unknown command: {command}")
                            conn.sendall(b'ACK_UNKNOWN')
                    
                    except Exception as e:
                        print(f"[{datetime.now().isoformat()}] [Error] Error handling connection from {addr}: {e}")

    except KeyboardInterrupt:
        print(f"\n[{datetime.now().isoformat()}] [Info] Shutting down server...")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Fatal] Main socket error: {e}")
    finally:
        # Ensure recording is stopped on any exit
        stop_recording()
        print(f"[{datetime.now().isoformat()}] [Info] Server shutdown complete.")

if __name__ == "__main__":
    main()

