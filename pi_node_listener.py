import json
import socket
import os
import re
import subprocess
import io
from datetime import datetime
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    from picamera2 import Picamera2
    from picamera2.encoders import H264Encoder, MJPEGEncoder
    from picamera2.outputs import FfmpegOutput, SplittableOutput, FileOutput
    PICAMERA_IMPORT_ERROR = None
except ImportError as import_exc:
    Picamera2 = None
    H264Encoder = None
    MJPEGEncoder = None
    FfmpegOutput = None
    SplittableOutput = None
    FileOutput = None
    PICAMERA_IMPORT_ERROR = import_exc

# --- Configuration ---
HOST = '0.0.0.0'  # Listen on all available network interfaces
PORT = 9090       # The port to listen on
VIDEO_PATH = os.path.expanduser('~/videos')  # Directory to save videos

# Preview streaming configuration (lo-res MJPEG stream)
PREVIEW_HOST = '0.0.0.0'
PREVIEW_PORT = 8081
PREVIEW_RESOLUTION = (640, 360)
PREVIEW_BITRATE = 4_000_000  # 4 Mbps MJPEG preview
PREVIEW_CLIENT_TIMEOUT = 10  # Seconds to wait for a frame before retrying

# Set segment time in milliseconds. 1200000ms = 20 minutes.
# This results in files of approx. 3.75GB (at 25Mbps), safely under the 4GB limit.
SEGMENT_TIME_MS = 1_200_000
VIDEO_BITRATE = 25_000_000  # 25 Mbps matches previous libcamera-vid usage
MAIN_STREAM_RESOLUTION = (1920, 1080)
MAIN_STREAM_FRAMERATE = 25
# ---------------------

# Global variables to keep track of the recording state
# We use globals so the main loop can manage the state
# based on socket commands.
picam2 = None
encoder = None
splittable_output = None
segment_rotator = None
recording_active = False
current_take_name = None
last_error_message = ""
log_file_handle = None
preview_output = None
preview_encoder = None
preview_stream_active = False
preview_server = None
preview_server_thread = None
preview_last_error = ""
camera_setup_lock = threading.RLock()
camera_pipeline_active = False
preview_frame_buffer = None

if PICAMERA_IMPORT_ERROR is not None:
    last_error_message = f"Picamera2 unavailable: {PICAMERA_IMPORT_ERROR}"


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


class MJPEGStreamingOutput(io.BufferedIOBase):
    """Thread-safe container that stores the most recent MJPEG frame."""

    def __init__(self):
        self._condition = threading.Condition()
        self._frame = None

    def writable(self):
        return True

    def write(self, buf):
        if not buf:
            return 0
        data = bytes(buf)
        with self._condition:
            self._frame = data
            self._condition.notify_all()
        return len(data)

    def close(self):
        with self._condition:
            self._frame = None
            self._condition.notify_all()
        return super().close()

    def get_latest_frame(self, timeout=None):
        with self._condition:
            if self._frame is None:
                self._condition.wait(timeout=timeout)
            return self._frame


def read_cpu_temperature():
    """Read CPU temperature for this node."""
    thermal_paths = [
        "/sys/class/thermal/thermal_zone0/temp",
        "/sys/devices/virtual/thermal/thermal_zone0/temp",
        "/sys/class/hwmon/hwmon0/temp1_input",
    ]

    for path in thermal_paths:
        try:
            with open(path, "r", encoding="utf-8") as handle:
                raw = handle.read().strip()
            if not raw:
                continue
            value = float(raw)
            if value > 200.0:
                value = value / 1000.0
            return value
        except (OSError, ValueError):
            continue

    try:
        output = subprocess.check_output(
            ["vcgencmd", "measure_temp"],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
        match = re.search(r"temp=([\d.]+)", output)
        if match:
            return float(match.group(1))
    except (FileNotFoundError, subprocess.CalledProcessError, ValueError):
        pass

    return None

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

    with camera_setup_lock:
        if picam2 is None:
            picam2 = Picamera2()
            video_config = picam2.create_video_configuration(
                main={"size": MAIN_STREAM_RESOLUTION},
                lores={"size": PREVIEW_RESOLUTION},
                controls={
                    "FrameRate": MAIN_STREAM_FRAMERATE,
                    "FrameDurationLimits": (int(1000000 / MAIN_STREAM_FRAMERATE), int(1000000 / MAIN_STREAM_FRAMERATE))
                }
            )
            picam2.configure(video_config)
            print(f"[{datetime.now().isoformat()}] [Info] Picamera2 configured for {MAIN_STREAM_RESOLUTION} main stream and {PREVIEW_RESOLUTION} preview.")

        if encoder is None:
            encoder = H264Encoder(bitrate=VIDEO_BITRATE)
            print(f"[{datetime.now().isoformat()}] [Info] H264 encoder initialized at {VIDEO_BITRATE} bps.")

        if not preview_stream_active:
            _start_preview_stream_locked()


def _start_preview_stream_locked():
    """Start the background MJPEG preview stream. Caller must hold camera_setup_lock."""
    global preview_output, preview_encoder, preview_stream_active, preview_last_error, camera_pipeline_active, preview_frame_buffer

    if MJPEGEncoder is None or FileOutput is None:
        preview_last_error = "MJPEGEncoder unavailable"
        return False

    if preview_stream_active:
        return True

    preview_frame_buffer = MJPEGStreamingOutput()
    preview_output = FileOutput(preview_frame_buffer)
    preview_encoder = MJPEGEncoder(bitrate=PREVIEW_BITRATE)

    if not camera_pipeline_active:
        try:
            picam2.start()
            camera_pipeline_active = True
            print(f"[{datetime.now().isoformat()}] [Info] Picamera2 pipeline started for preview streaming.")
        except RuntimeError as start_error:
            if "already" in str(start_error).lower():
                camera_pipeline_active = True
                print(f"[{datetime.now().isoformat()}] [Info] Picamera2 pipeline already running.")
            else:
                preview_last_error = f"Failed to start camera pipeline: {start_error}"
                preview_output = None
                preview_encoder = None
                return False
        except Exception as start_error:
            preview_last_error = f"Failed to start camera pipeline: {start_error}"
            preview_output = None
            preview_encoder = None
            return False

    try:
        picam2.start_encoder(preview_encoder, preview_output, name="lores")
    except Exception as encoder_error:
        preview_last_error = f"Failed to start preview encoder: {encoder_error}"
        preview_output = None
        preview_encoder = None
        return False

    preview_stream_active = True
    preview_last_error = ""
    print(f"[{datetime.now().isoformat()}] [Info] Preview MJPEG stream running on lo-res pipeline.")
    return True


def ensure_preview_stream():
    """Ensure the preview infrastructure is operational."""
    global preview_last_error
    try:
        initialize_camera()
    except RuntimeError as init_error:
        preview_last_error = str(init_error)
        return False

    with camera_setup_lock:
        if not preview_stream_active:
            _start_preview_stream_locked()
        return preview_stream_active


class PreviewRequestHandler(BaseHTTPRequestHandler):
    """Simple MJPEG streaming handler for remote preview."""

    def do_GET(self):
        if self.path not in ("/", "/preview.mjpg"):
            self.send_error(HTTPStatus.NOT_FOUND, "Preview endpoint not found.")
            return

        if PICAMERA_IMPORT_ERROR is not None:
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, f"Preview offline: {PICAMERA_IMPORT_ERROR}")
            return

        if not ensure_preview_stream() or preview_frame_buffer is None:
            message = preview_last_error or "Preview stream not available."
            self.send_error(HTTPStatus.SERVICE_UNAVAILABLE, message)
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Age", "0")
        self.send_header("Cache-Control", "no-cache, private")
        self.send_header("Pragma", "no-cache")
        self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=FRAME")
        self.end_headers()

        try:
            while True:
                frame = preview_frame_buffer.get_latest_frame(timeout=PREVIEW_CLIENT_TIMEOUT)
                if frame is None:
                    continue
                self.wfile.write(b"--FRAME\r\n")
                self.wfile.write(b"Content-Type: image/jpeg\r\n")
                self.wfile.write(f"Content-Length: {len(frame)}\r\n\r\n".encode("ascii"))
                self.wfile.write(frame)
                self.wfile.write(b"\r\n")
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception as stream_error:
            print(f"[{datetime.now().isoformat()}] [Warning] Preview stream error: {stream_error}")

    def log_message(self, format, *args):
        # Silence default HTTP request logging to keep CLI clean.
        return


def start_preview_server():
    """Start the HTTP server that serves MJPEG preview frames."""
    global preview_server, preview_server_thread
    if preview_server is not None:
        return

    try:
        preview_server = ThreadingHTTPServer((PREVIEW_HOST, PREVIEW_PORT), PreviewRequestHandler)
    except OSError as server_error:
        print(f"[{datetime.now().isoformat()}] [Error] Failed to start preview server: {server_error}")
        preview_server = None
        return

    preview_server.daemon_threads = True
    preview_server_thread = threading.Thread(
        target=preview_server.serve_forever,
        name="PreviewHTTPServer",
        daemon=True
    )
    preview_server_thread.start()
    print(f"[{datetime.now().isoformat()}] [Info] Preview server listening on {PREVIEW_HOST}:{PREVIEW_PORT}.")


def shutdown_preview_server():
    """Stop the preview HTTP server."""
    global preview_server, preview_server_thread
    stop_preview_stream()
    if preview_server:
        try:
            preview_server.shutdown()
            preview_server.server_close()
        except Exception as server_error:
            print(f"[{datetime.now().isoformat()}] [Warning] Error shutting down preview server: {server_error}")
        finally:
            preview_server = None
            preview_server_thread = None


def stop_preview_stream():
    """Stops the MJPEG encoder pipeline."""
    global preview_stream_active, preview_encoder, preview_output, preview_frame_buffer
    if not preview_stream_active:
        preview_output = None
        preview_encoder = None
        preview_frame_buffer = None
        return

    if picam2 and preview_encoder:
        try:
            picam2.stop_encoder(preview_encoder)
        except Exception as stop_error:
            print(f"[{datetime.now().isoformat()}] [Warning] Failed to stop preview encoder: {stop_error}")

    preview_stream_active = False
    preview_encoder = None
    preview_output = None
    preview_frame_buffer = None


def start_recording(take_name):
    """Starts the Picamera2 recording process with a given take name."""
    global recording_active, log_file_handle, splittable_output, segment_rotator, current_take_name, last_error_message

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
        current_take_name = take_name
        last_error_message = ""

        print(f"[{datetime.now().isoformat()}] [Success] Recording started using Picamera2.")
        print(f"[{datetime.now().isoformat()}] [Info] Marker file created: {log_filename}")

    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Failed to start recording: {e}")
        recording_active = False
        splittable_output = None
        segment_rotator = None
        last_error_message = str(e)
        current_take_name = None
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
    global recording_active, log_file_handle, splittable_output, segment_rotator, current_take_name, last_error_message

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

        last_error_message = ""

    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Error stopping recording: {e}")
        last_error_message = str(e)

    finally:
        recording_active = False
        splittable_output = None
        current_take_name = None
        if log_file_handle:
            try:
                log_file_handle.write(f"# Recording stopped at: {datetime.now().isoformat()}\n")
                log_file_handle.close()
            except Exception as log_error:
                print(f"[{datetime.now().isoformat()}] [Error] Failed to close log file: {log_error}")
            finally:
                log_file_handle = None

        # Make sure the preview restarts if the camera pipeline stopped with the recording.
        try:
            ensure_preview_stream()
        except Exception as preview_error:
            print(f"[{datetime.now().isoformat()}] [Warning] Could not restart preview after stop: {preview_error}")


def mark_timecode(note=None):
    """Writes a marker (current timestamp) to the log file."""
    global last_error_message
    if not log_file_handle or log_file_handle.closed:
        print(f"[{datetime.now().isoformat()}] [Warning] Cannot mark timecode, not recording or log is closed.")
        return

    try:
        marker_time = datetime.now().isoformat()
        entry = f"MARK: {marker_time}"
        if note:
            entry += f" | {note}"
        log_file_handle.write(f"{entry}\n")
        log_file_handle.flush()  # Ensure it's written immediately
        if note:
            print(f"[{datetime.now().isoformat()}] [Info] Marked timecode: {marker_time} | {note}")
        else:
            print(f"[{datetime.now().isoformat()}] [Info] Marked timecode: {marker_time}")

    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Error] Failed to write marker: {e}")
        last_error_message = str(e)


def main():
    """Main function to run the socket server."""
    global last_error_message
    # Ensure video directory exists on startup
    try:
        os.makedirs(VIDEO_PATH, exist_ok=True)
        print(f"[{datetime.now().isoformat()}] [Info] Video storage directory: {VIDEO_PATH}")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Fatal] Could not create video directory: {e}. Exiting.")
        return

    start_preview_server()

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
                        elif command.startswith('MARK'):
                            note = None
                            if command.startswith('MARK:'):
                                note = command.split(':', 1)[1].strip()
                            print(f"[{datetime.now().isoformat()}] [Info] Received MARK command{f' with note: {note}' if note else '.'}")
                            mark_timecode(note or None)
                            conn.sendall(b'ACK_MARK')
                        elif command == 'STATUS':
                            cpu_temp = read_cpu_temperature()
                            status_payload = {
                                "recording": recording_active,
                                "take_name": current_take_name,
                                "last_error": last_error_message,
                                "picamera_available": PICAMERA_IMPORT_ERROR is None,
                                "preview_available": preview_stream_active,
                                "preview_port": PREVIEW_PORT,
                                "preview_error": preview_last_error,
                                "cpu_temp_c": cpu_temp,
                            }
                            try:
                                conn.sendall(f"STATUS:{json.dumps(status_payload)}".encode('utf-8'))
                            except Exception as send_error:
                                print(f"[{datetime.now().isoformat()}] [Error] Failed to send STATUS response: {send_error}")
                        else:
                            print(f"[{datetime.now().isoformat()}] [Warning] Unknown command: {command}")
                            conn.sendall(b'ACK_UNKNOWN')

                    except Exception as e:
                        print(f"[{datetime.now().isoformat()}] [Error] Error handling connection from {addr}: {e}")
                        last_error_message = str(e)

    except KeyboardInterrupt:
        print(f"\n[{datetime.now().isoformat()}] [Info] Shutting down server...")
    except Exception as e:
        print(f"[{datetime.now().isoformat()}] [Fatal] Main socket error: {e}")
    finally:
        # Ensure recording is stopped on any exit
        stop_recording()
        shutdown_preview_server()
        print(f"[{datetime.now().isoformat()}] [Info] Server shutdown complete.")


if __name__ == "__main__":
    main()
