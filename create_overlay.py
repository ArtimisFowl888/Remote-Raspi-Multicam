import os
import sys
import glob
import re
import datetime
import argparse
import subprocess

def parse_timestamp(ts_str):
    """Parses YYYYMMDD_HHMMSS or ISO format."""
    try:
        return datetime.datetime.strptime(ts_str, "%Y%m%d_%H%M%S")
    except ValueError:
        pass
    try:
        return datetime.datetime.fromisoformat(ts_str)
    except ValueError:
        pass
    return None

def format_srt_time(td):
    """Formats a timedelta as HH:MM:SS,mmm for SRT."""
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    milliseconds = int(td.microseconds / 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"

def generate_srt(srt_path, markers, start_time_dt, duration_sec):
    """
    Generates SRT file for the video.
    markers: list of (datetime, note)
    start_time_dt: video start datetime
    duration_sec: video duration in seconds
    """
    points = set()
    points.add(0.0)
    points.add(duration_sec)
    
    events = [] # (start_offset, end_offset, text)
    state_changes = [] # (offset, key, value)
    
    # Process all markers to build state/event lists
    for mark_time, note in markers:
        offset = (mark_time - start_time_dt).total_seconds()
        
        # Parse note for key:value pairs
        if ":" in note:
            # Split by comma to handle multiple updates "spd:10, fm:stab"
            subparts = note.split(',')
            for sp in subparts:
                if ":" in sp:
                    k, v = sp.split(':', 1)
                    state_changes.append((offset, k.strip().upper(), v.strip()))
                    points.add(offset)
                else:
                    # Mixed content without colon -> Event
                    events.append((offset, offset + 5.0, sp.strip()))
                    points.add(offset)
                    points.add(offset + 5.0)
        else:
            # Pure event
            events.append((offset, offset + 5.0, note))
            points.add(offset)
            points.add(offset + 5.0)

    # Filter points to be within [0, duration]
    sorted_points = sorted([p for p in points if 0 <= p <= duration_sec])
    sorted_points = sorted(list(set(sorted_points)))
    
    # Sort state changes by time for replay
    state_changes.sort(key=lambda x: x[0])
    
    srt_entries = []
    
    for i in range(len(sorted_points) - 1):
        t_start = sorted_points[i]
        t_end = sorted_points[i+1]
        
        if t_end - t_start < 0.1:
            continue
            
        mid_point = (t_start + t_end) / 2
        
        # Determine state at mid_point
        current_state = {}
        for off, k, v in state_changes:
            if off <= mid_point:
                current_state[k] = v
            else:
                break
        
        # Determine active events at mid_point
        active_events = []
        for start, end, text in events:
            if start <= mid_point < end:
                active_events.append(text)
                
        # Construct Text
        lines = []
        
        # Telemetry Line
        telemetry_parts = []
        for k, v in current_state.items():
            telemetry_parts.append(f"{k}: {v}")
        
        if telemetry_parts:
            lines.append(" | ".join(telemetry_parts))
            
        # Events Line
        if active_events:
            lines.append(" | ".join(active_events))
            
        full_text = "\n".join(lines)
        
        if full_text:
            srt_entries.append((t_start, t_end, full_text))

    # Write SRT
    with open(srt_path, "w", encoding="utf-8") as f:
        for idx, (start, end, text) in enumerate(srt_entries, 1):
            start_td = datetime.timedelta(seconds=start)
            end_td = datetime.timedelta(seconds=end)
            f.write(f"{idx}\n")
            f.write(f"{format_srt_time(start_td)} --> {format_srt_time(end_td)}\n")
            f.write(f"{text}\n\n")
            
    print(f"Generated SRT: {srt_path}")
    return srt_path

def create_transparent_overlay(take_path, duration=None, position="bottom"):
    take_name = os.path.basename(take_path)
    print(f"Processing take: {take_name}")
    
    # 1. Find Markers
    marker_files = glob.glob(os.path.join(take_path, "*", "*_markers.txt"))
    if not marker_files:
        print("No marker files found.")
        return

    # Use the first marker file found (assuming all cameras have same markers)
    marker_file = marker_files[0]
    take_markers = []
    with open(marker_file, "r") as f:
        for line in f:
            if line.startswith("MARK:"):
                parts = line.strip().split("MARK:", 1)[1].strip().split("|", 1)
                ts_str = parts[0].strip()
                note = parts[1].strip() if len(parts) > 1 else ""
                dt = parse_timestamp(ts_str)
                if dt:
                    take_markers.append((dt, note))
    
    if not take_markers:
        print("No markers found in file.")
        return

    # 2. Determine Start Time and Duration
    # Try to find a video file to get start time
    video_files = glob.glob(os.path.join(take_path, "*", "*.mp4"))
    start_time = None
    
    if video_files:
        # Parse timestamp from filename
        match = re.search(r"_(\d{8}_\d{6})_", os.path.basename(video_files[0]))
        if match:
            start_time = parse_timestamp(match.group(1))
    
    if not start_time:
        # Fallback: Use first marker time
        start_time = take_markers[0][0]
        print("Could not determine start time from video files. Using first marker time.")

    if not duration:
        # Default duration: 10 minutes or until last marker + 10s
        last_marker_time = take_markers[-1][0]
        duration = (last_marker_time - start_time).total_seconds() + 10.0
        if duration < 60: duration = 60.0 # Minimum 1 minute
    
    print(f"Start Time: {start_time}")
    print(f"Duration: {duration} seconds")

    # 3. Generate SRT
    srt_path = os.path.join(take_path, f"{take_name}_overlay.srt")
    generate_srt(srt_path, take_markers, start_time, duration)

    # 4. Generate Transparent Video
    output_mov = os.path.join(take_path, f"{take_name}_overlay.mov")
    
    # Map position to Alignment
    align_map = {
        "bottom": 2,
        "top": 6,
        "top-left": 5,
        "top-right": 7,
        "bottom-left": 1,
        "bottom-right": 3,
        "center": 10
    }
    alignment = align_map.get(position, 2)
    
    srt_path_clean = srt_path.replace("\\", "/").replace(":", "\\:")

    cmd = [
        "ffmpeg", "-y",
        "-f", "lavfi",
        "-i", f"color=c=black@0.0:s=1920x1080:d={duration}",
        "-vf", f"subtitles='{srt_path_clean}':force_style='Alignment={alignment},MarginV=20,FontSize=24'",
        "-c:v", "prores_ks",
        "-profile:v", "4444",
        "-pix_fmt", "yuva444p10le",
        output_mov
    ]
    
    print(f"Generating transparent overlay: {output_mov}")
    try:
        subprocess.run(cmd, check=True)
        print("Success!")
    except subprocess.CalledProcessError as e:
        print(f"Error: {e}")

def main():
    parser = argparse.ArgumentParser(description="Create transparent telemetry overlay video.")
    parser.add_argument("take_path", help="Path to the take directory")
    parser.add_argument("--duration", type=float, help="Duration in seconds (optional)")
    parser.add_argument("--position", default="bottom", choices=["bottom", "top", "top-left", "top-right", "bottom-left", "bottom-right", "center"], help="Position of text")
    args = parser.parse_args()
    
    if not os.path.isdir(args.take_path):
        print(f"Error: {args.take_path} is not a directory.")
        return

    create_transparent_overlay(args.take_path, args.duration, args.position)

if __name__ == "__main__":
    main()
