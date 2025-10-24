import socket
import os
from datetime import datetime
import threading

try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder
    from picamera2.outputs import FfmpegOutput, SplittableOutput
    PICAMERA_IMPORT_ERROR = None
except ImportError as import_exc:
    Picamera2 = None
    H264Encoder = None
    FfmpegOutput = None
    SplittableOutput = None
    PICAMERA_IMPORT_ERROR = import_exc

# --- Configuration ---
HOST = '0.0.0.0'  # Listen on all available network interfaces
PORT = 9090       # The port to listen on
VIDEO_PATH = os.path.expanduser('~/videos')  # Directory to save videos

# Set segment time in milliseconds. 1200000ms = 20 minutes.
# This results in files of approx. 3.75GB (at 25Mbps), safely under the 4GB limit.
SEGMENT_TIME_MS = 1_200_000
VIDEO_BITRATE = 25_000_000  # 25 Mbps matches previous libcamera-vid usage
# ---------------------

# Global variables to keep track of the recording state
# We use globals so the main loop can manage the state
# based on socket commands.
picam2 = None
encoder = None
splittable_output = None
segment_rotator = None
recording_active = False
log_file_handle = None


class SegmentRotator:
    """Background helper that rotates output files every N seconds."""

    def __init__(self, template, interval_seconds, output_wrapper, on_segment_created=None):
        self.template = template
        self.interval = max(interval_seconds, 1)  # Guard against zero/negative durations
        self.output_wrapper = output_wrapper
        self.on_segment_created = on_segment_created
        self._stop_event = threading.Event()
        self._thread = None
        self._segment_index = 0

    def start(self, starting_index=0):
        self._segment_index = starting_index
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="SegmentRotator", daemon=True)
        self._thread.start()

    def stop(self):
        if self._thread:
            self._stop_event.set()
            self._thread.join(timeout=self.interval)
            self._thread = None

    def _run(self):
        while not self._stop_event.wait(self.interval):
            try:
                self._segment_index += 1
                next_filename = self.template % self._segment_index
                new_output = FfmpegOutput(next_filename, audio=False)
                self.output_wrapper.split_output(new_output, wait_for_keyframe=True)
                if self.on_segment_created:
                    self.on_segment_created(next_filename, self._segment_index)
                print(f"[{datetime.now().isoformat()}] [Info] Rotated to new segment: {next_filename}")
            except Exception as e:
                print(f"[{datetime.now().isoformat()}] [Error] Failed to rotate segment: {e}")
                break


def get_pi_ip():
    """Gets the Pi's own IP address to use in filenames."""
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("8.8.8.8", 80))  # Connect to a known external IP
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Could not get IP: {e}. Defaulting to 'local-pi'.")
        return "local-pi"


def initialize_camera():
    """Lazy-initialize the Picamera2 instance and encoder."""
    global picam2, encoder

    if PICAMERA_IMPORT_ERROR is not None:
        raise RuntimeError(f"Picamera2 not available: {PICAMERA_IMPORT_ERROR}")

    if picam2 is None:
        picam2 = Picamera2()
        video_config = picam2.create_video_configuration()
        picam2.configure(video_config)
        print(f"[{datetime.now().isoformat()}] [Info] Picamera2 configured for video recording.")

    if encoder is None:
        encoder = H264Encoder(bitrate=VIDEO_BITRATE)
        print(f"[{datetime.now().isoformat()}] [Info] H264 encoder initialized at {VIDEO_BITRATE} bps.")


def start_recording(take_name):
    """Starts the Picamera2 recording process with a given take name."""
    global recording_active, log_file_handle, splittable_output, segment_rotator

    if recording_active:
        print(f"[{datetime.now().isoformat()}] [Warning] Already recording. Ignoring START command.")
        return

    try:
        initialize_camera()

        # Get identifying info for the filename
        pi_ip = get_pi_ip()
        safe_ip = pi_ip.replace('.', '_')  # Make IP filesystem-safe
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

        # Base filename format: <TakeName>_<SafeIP>_<Timestamp>
        base_filename = f"{take_name}_{safe_ip}_{timestamp}"

        # Picamera2 will rotate segments following the template suffix %06d
        video_filename_template = os.path.join(VIDEO_PATH, f"{base_filename}_%06d.mp4")
        log_filename = os.path.join(VIDEO_PATH, f"{base_filename}_markers.txt")

        first_segment = video_filename_template % 0
        mp4_output = FfmpegOutput(first_segment, audio=False)
        splittable_output = SplittableOutput(mp4_output)

        picam2.start_recording(encoder, splittable_output)

        # Open the marker log file (only ONE log file per take)
        log_file_handle = open(log_filename, 'w')
        log_file_handle.write(f"# Markers for {base_filename}\n")
        log_file_handle.write(f"# Recording started at: {datetime.now().isoformat()}\n")
        log_file_handle.write(f"# Video segmented every {SEGMENT_TIME_MS} ms.\n")
        log_file_handle.write(f"# Segment 000000 file: {first_segment}\n")
        log_file_handle.flush()

        segment_seconds = SEGMENT_TIME_MS / 1000.0

        def on_segment_created(filename, index):
            if log_file_handle and not log_file_handle.closed:
                try:
                    log_file_handle.write(f"# Segment {index:06d} file: {filename}\n")
                    log_file_handle.flush()
                except Exception as log_error:
                    print(f"[{datetime.now().isoformat()}] [Error] Failed to log segment info: {log_error}")

        segment_rotator = SegmentRotator(
            template=video_filename_template,
            interval_seconds=segment_seconds,
            output_wrapper=splittable_output,
            on_segment_created=on_segment_created
        )
        segment_rotator.start(starting_index=0)

        recording_active = True

        print(f"[{datetime.now().isoformat()}] [Success] Recording started using Picamera2.")
        print(f"[{datetime.now().isoformat()}] [Info] Marker file created: {log_filename}")

    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Failed to start recording: {e}")
        recording_active = False
        splittable_output = None
        segment_rotator = None
        if log_file_handle and not log_file_handle.closed:
            try:
                log_file_handle.write(f"# Recording start failed at: {datetime.now().isoformat()}\n")
                log_file_handle.flush()
                log_file_handle.close()
            except Exception:
                pass
        log_file_handle = None


def stop_recording():
    """Stops the recording process gracefully."""
    global recording_active, log_file_handle, splittable_output, segment_rotator

    if not recording_active:
        print(f"[{datetime.now().isoformat()}] [Warning] Not recording. Ignoring STOP command.")
        return

    try:
        print(f"[{datetime.now().isoformat()}] [Info] Stopping recording...")

        active_rotator = segment_rotator
        segment_rotator = None
        if active_rotator:
            try:
                active_rotator.stop()
            except Exception as rotator_error:
                print(f"[{datetime.now().isoformat()}] [Warning] Error stopping segment rotator: {rotator_error}")

        if picam2:
            try:
                picam2.stop_recording()
            except Exception as camera_error:
                print(f"[{datetime.now().isoformat()}] [Error] Picamera2 stop failed: {camera_error}")
            else:
                print(f"[{datetime.now().isoformat()}] [Success] Recording stopped.")
        else:
            print(f"[{datetime.now().isoformat()}] [Success] Recording stopped (camera already released).")

    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Error stopping recording: {e}")

    finally:
        recording_active = False
        splittable_output = None
        if log_file_handle:
            try:
                log_file_handle.write(f"# Recording stopped at: {datetime.now().isoformat()}\n")
                log_file_handle.close()
            except Exception as log_error:
                print(f"[{datetime.now().isoformat()}] [Error] Failed to close log file: {log_error}")
            finally:
                log_file_handle = None


def mark_timecode():
    """Writes a marker (current timestamp) to the log file."""
    if not log_file_handle or log_file_handle.closed:
        print(f"[{datetime.now().isoformat()}] [Warning] Cannot mark timecode, not recording or log is closed.")
        return

    try:
        marker_time = datetime.now().isoformat()
        log_file_handle.write(f"MARK: {marker_time}\n")
        log_file_handle.flush()  # Ensure it's written immediately
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
