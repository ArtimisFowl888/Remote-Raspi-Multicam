import os
import sys
import glob
import re
import datetime
import argparse
import subprocess
import xml.etree.ElementTree as ET
from xml.dom import minidom

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

def get_video_duration(file_path):
    """
    Estimates video duration using ffprobe.
    Defaults to 10.0 if not found.
    """
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return float(result.stdout.strip())
    except Exception:
        # print(f"Warning: Could not get duration for {file_path} (ffprobe not found?). Using default 10s.")
        return 10.0

def format_srt_time(td):
    """Formats a timedelta as HH:MM:SS,mmm for SRT."""
    total_seconds = int(td.total_seconds())
    hours = total_seconds // 3600
    minutes = (total_seconds % 3600) // 60
    seconds = total_seconds % 60
    milliseconds = int(td.microseconds / 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{milliseconds:03}"

def generate_xmp(video_path, markers, start_time_dt):
    """
    Generates an XMP sidecar file for the given video.
    """
    xmp_path = os.path.splitext(video_path)[0] + ".xmp"
    
    xmp_content = f"""<x:xmpmeta xmlns:x="adobe:ns:meta/" x:xmptk="Adobe XMP Core 5.6-c137 79.159768, 2016/08/11-13:24:42        ">
 <rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#">
  <rdf:Description rdf:about=""
    xmlns:xmp="http://ns.adobe.com/xap/1.0/"
    xmlns:xmpDM="http://ns.adobe.com/xmp/1.0/DynamicMedia/"
    xmlns:stDim="http://ns.adobe.com/xap/1.0/sType/Dimensions#"
    xmlns:xmpMM="http://ns.adobe.com/xap/1.0/mm/"
    xmlns:stEvt="http://ns.adobe.com/xap/1.0/sType/ResourceEvent#"
    xmlns:dc="http://purl.org/dc/elements/1.1/">
   <xmpDM:Tracks>
    <rdf:Bag>
     <rdf:li>
      <rdf:Description xmpDM:trackType="Comment" xmpDM:frameRate="f254016000000" xmpDM:name="Comment">
       <xmpDM:markers>
        <rdf:Seq>
"""
    
    for mark_time, note in markers:
        delta = (mark_time - start_time_dt).total_seconds()
        if delta < 0:
            continue 
            
        start_ticks = int(delta * 254016000000)
        duration_ticks = 0
        note_safe = note if note else "Marker"
        
        xmp_content += f"""         <rdf:li rdf:parseType="Resource">
          <xmpDM:startTime>{start_ticks}</xmpDM:startTime>
          <xmpDM:duration>{duration_ticks}</xmpDM:duration>
          <xmpDM:name>{note_safe}</xmpDM:name>
          <xmpDM:comment>{note_safe}</xmpDM:comment>
         </rdf:li>
"""

    xmp_content += """        </rdf:Seq>
       </xmpDM:markers>
      </rdf:Description>
     </rdf:li>
    </rdf:Bag>
   </xmpDM:Tracks>
  </rdf:Description>
 </rdf:RDF>
</x:xmpmeta>
"""
    
    with open(xmp_path, "w", encoding="utf-8") as f:
        f.write(xmp_content)
    print(f"Generated XMP: {xmp_path}")

def generate_srt(video_path, markers, start_time_dt, duration_sec):
    """
    Generates SRT file for the video.
    markers: list of (datetime, note)
    start_time_dt: video start datetime
    duration_sec: video duration in seconds
    """
    srt_path = os.path.splitext(video_path)[0] + ".srt"
    
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

def burn_subtitles(video_path, srt_path, position, lossless=False):
    """
    Burns subtitles into video using FFmpeg.
    Returns the path to the new video file.
    """
    burned_path = os.path.splitext(video_path)[0] + "_burned.mp4"
    
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
    
    # Use forward slashes for FFmpeg filter path to avoid escaping issues
    srt_path_clean = srt_path.replace("\\", "/").replace(":", "\\:")
    
    # FFmpeg command
    cmd = [
        "ffmpeg", "-y",
        "-i", video_path,
        "-vf", f"subtitles='{srt_path_clean}':force_style='Alignment={alignment},MarginV=20,FontSize=24'",
        "-c:a", "copy"
    ]

    if lossless:
        # CRF 18 is generally considered visually lossless
        cmd.extend(["-c:v", "libx264", "-preset", "veryfast", "-crf", "18"])
    else:
        # Default FFmpeg (usually CRF 23)
        cmd.extend(["-c:v", "libx264", "-preset", "fast"])

    cmd.append(burned_path)
    
    print(f"Burning subtitles: {video_path} -> {burned_path} (Position: {position}, Lossless: {lossless})")
    try:
        # Suppress output unless error
        subprocess.run(cmd, check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        return burned_path
    except subprocess.CalledProcessError as e:
        print(f"Error burning subtitles: {e.stderr.decode()}")
        return None

def generate_xml(take_name, take_dir, clips_by_ip, all_markers):
    """
    Generates FCP7 XML for the take.
    """
    global_start = None
    for ip, clips in clips_by_ip.items():
        for clip in clips:
            if global_start is None or clip['start_time'] < global_start:
                global_start = clip['start_time']
                
    if global_start is None:
        return

    root = ET.Element("xmeml", version="4")
    project = ET.SubElement(root, "project")
    ET.SubElement(project, "name").text = take_name
    children = ET.SubElement(project, "children")
    
    sequence = ET.SubElement(children, "sequence", id="sequence-1")
    ET.SubElement(sequence, "name").text = take_name
    ET.SubElement(sequence, "duration").text = "10000" 
    
    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = "25"
    ET.SubElement(rate, "ntsc").text = "FALSE"
    
    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    
    format_node = ET.SubElement(video, "format")
    samplecharacteristics = ET.SubElement(format_node, "samplecharacteristics")
    rate_sc = ET.SubElement(samplecharacteristics, "rate")
    ET.SubElement(rate_sc, "timebase").text = "25"
    ET.SubElement(samplecharacteristics, "width").text = "1920"
    ET.SubElement(samplecharacteristics, "height").text = "1080"
    ET.SubElement(samplecharacteristics, "pixelaspectratio").text = "square"

    sorted_ips = sorted(clips_by_ip.keys())
    
    # Grid positions for 2x2 Split View
    # Premiere Pro / FCP7 XML Coordinate System for "Basic Motion":
    # Center = (0, 0)
    # Range is typically -100 to 100 (Percentage of frame from center to edge?)
    # Top-Left Quadrant Center: x=-50, y=-50
    # Top-Right Quadrant Center: x=50, y=-50
    # Bottom-Left Quadrant Center: x=-50, y=50
    # Bottom-Right Quadrant Center: x=50, y=50
    
    grid_positions = [
        (-50, -50), # Index 0: Top-Left
        (50, -50),  # Index 1: Top-Right
        (-50, 50),  # Index 2: Bottom-Left
        (50, 50)    # Index 3: Bottom-Right
    ]

    for i, ip in enumerate(sorted_ips):
        track = ET.SubElement(video, "track")
        clips = sorted(clips_by_ip[ip], key=lambda x: x['start_time'])
        
        # Determine position for this camera angle
        pos_x, pos_y = (0, 0)
        if i < len(grid_positions):
            pos_x, pos_y = grid_positions[i]
        
        for clip in clips:
            offset_seconds = (clip['start_time'] - global_start).total_seconds()
            start_frame = int(offset_seconds * 25)
            duration_frames = int(clip['duration'] * 25)
            end_frame = start_frame + duration_frames
            
            clipitem = ET.SubElement(track, "clipitem", id=f"clipitem-{ip}-{start_frame}")
            ET.SubElement(clipitem, "name").text = os.path.basename(clip['path'])
            ET.SubElement(clipitem, "duration").text = str(duration_frames)
            
            rate_ci = ET.SubElement(clipitem, "rate")
            ET.SubElement(rate_ci, "timebase").text = "25"
            
            ET.SubElement(clipitem, "start").text = str(start_frame)
            ET.SubElement(clipitem, "end").text = str(end_frame)
            
            file_node = ET.SubElement(clipitem, "file", id=f"file-{os.path.basename(clip['path'])}")
            ET.SubElement(file_node, "name").text = os.path.basename(clip['path'])
            ET.SubElement(file_node, "pathurl").text = "file://localhost/" + clip['path'].replace("\\", "/")
            
            rate_f = ET.SubElement(file_node, "rate")
            ET.SubElement(rate_f, "timebase").text = "25"
            
            media_f = ET.SubElement(file_node, "media")
            video_f = ET.SubElement(media_f, "video")
            ET.SubElement(video_f, "duration").text = str(duration_frames)

            # Apply Split View Filter (Basic Motion)
            filter_node = ET.SubElement(clipitem, "filter")
            effect_node = ET.SubElement(filter_node, "effect")
            ET.SubElement(effect_node, "name").text = "Basic Motion"
            ET.SubElement(effect_node, "effectid").text = "basic motion"
            ET.SubElement(effect_node, "effectcategory").text = "motion"
            ET.SubElement(effect_node, "effecttype").text = "motion"
            ET.SubElement(effect_node, "mediatype").text = "video"
            
            # Scale Parameter
            scale_param = ET.SubElement(effect_node, "parameter")
            ET.SubElement(scale_param, "name").text = "Scale"
            ET.SubElement(scale_param, "parameterid").text = "scale"
            ET.SubElement(scale_param, "valuemin").text = "0"
            ET.SubElement(scale_param, "valuemax").text = "1000"
            
            scale_kf = ET.SubElement(scale_param, "keyframe")
            ET.SubElement(scale_kf, "when").text = "0"
            ET.SubElement(scale_kf, "value").text = "50"
            
            # Center Parameter
            center_param = ET.SubElement(effect_node, "parameter")
            ET.SubElement(center_param, "name").text = "Center"
            ET.SubElement(center_param, "parameterid").text = "center"
            
            center_kf = ET.SubElement(center_param, "keyframe")
            ET.SubElement(center_kf, "when").text = "0"
            val_node = ET.SubElement(center_kf, "value")
            ET.SubElement(val_node, "horiz").text = str(pos_x)
            ET.SubElement(val_node, "vert").text = str(pos_y)

    if all_markers:
        for mark_time, note in all_markers:
            offset_seconds = (mark_time - global_start).total_seconds()
            if offset_seconds >= 0:
                frame = int(offset_seconds * 25)
                marker = ET.SubElement(sequence, "marker")
                ET.SubElement(marker, "name").text = note if note else "Marker"
                ET.SubElement(marker, "in").text = str(frame)
                ET.SubElement(marker, "out").text = str(frame)
                ET.SubElement(marker, "comment").text = note if note else ""

    xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
    xml_path = os.path.join(take_dir, f"{take_name}.xml")
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(xml_str)
    print(f"Generated XML: {xml_path}")

def process_take(take_path, burn=False, position="bottom", lossless=False):
    take_name = os.path.basename(take_path)
    print(f"Processing take: {take_name}")
    
    clips_by_ip = {}
    all_markers = []
    
    for ip_folder in os.listdir(take_path):
        ip_path = os.path.join(take_path, ip_folder)
        if not os.path.isdir(ip_path):
            continue
            
        marker_files = glob.glob(os.path.join(ip_path, "*_markers.txt"))
        take_markers = []
        if marker_files:
            with open(marker_files[0], "r") as f:
                for line in f:
                    if line.startswith("MARK:"):
                        parts = line.strip().split("MARK:", 1)[1].strip().split("|", 1)
                        ts_str = parts[0].strip()
                        note = parts[1].strip() if len(parts) > 1 else ""
                        dt = parse_timestamp(ts_str)
                        if dt:
                            take_markers.append((dt, note))
                            all_markers.append((dt, note))
        
        video_files = glob.glob(os.path.join(ip_path, "*.mp4"))
        if not video_files:
             video_files = glob.glob(os.path.join(ip_path, "*.h264"))
             
        if not video_files:
            continue
            
        clips_by_ip[ip_folder] = []
        
        for vid in video_files:
            # Skip already burned files to avoid double processing
            if "_burned" in vid:
                continue

            match = re.search(r"_(\d{8}_\d{6})_", os.path.basename(vid))
            if match:
                ts_str = match.group(1)
                start_time = parse_timestamp(ts_str)
                if start_time:
                    duration = get_video_duration(vid)
                    
                    seg_match = re.search(r"_(\d{6})\.(mp4|h264)$", os.path.basename(vid))
                    if seg_match:
                        seg_idx = int(seg_match.group(1))
                        segment_duration = 1200.0 
                        start_time = start_time + datetime.timedelta(seconds=seg_idx * segment_duration)
                    
                    # Generate Sidecar (XMP)
                    generate_xmp(vid, take_markers, start_time)
                    
                    # Generate SRT
                    srt_path = generate_srt(vid, take_markers, start_time, duration)
                    
                    final_vid_path = vid
                    
                    # Burn-in if requested
                    if burn:
                        burned_path = burn_subtitles(vid, srt_path, position, lossless)
                        if burned_path:
                            final_vid_path = burned_path
                    
                    clips_by_ip[ip_folder].append({
                        'path': final_vid_path,
                        'start_time': start_time,
                        'duration': duration
                    })

    generate_xml(take_name, take_path, clips_by_ip, all_markers)

def main():
    parser = argparse.ArgumentParser(description="Process multicam takes for Premiere Pro.")
    parser.add_argument("directory", help="Directory containing takes (e.g. video_downloads)")
    parser.add_argument("--burn", action="store_true", help="Burn subtitles into video files")
    parser.add_argument("--position", default="bottom", choices=["bottom", "top", "top-left", "top-right", "bottom-left", "bottom-right", "center"], help="Position of burned subtitles")
    parser.add_argument("--lossless", action="store_true", help="Use lossless compression (CRF 18) for burned video")
    args = parser.parse_args()
    
    base_dir = args.directory
    if not os.path.isdir(base_dir):
        print(f"Error: {base_dir} is not a directory.")
        return

    for item in os.listdir(base_dir):
        path = os.path.join(base_dir, item)
        if os.path.isdir(path):
            has_ips = False
            for sub in os.listdir(path):
                if re.match(r"\d+\.\d+\.\d+\.\d+", sub) or re.match(r"\d+_\d+_\d+_\d+", sub):
                    has_ips = True
                    break
            
            if has_ips:
                process_take(path, burn=args.burn, position=args.position, lossless=args.lossless)

if __name__ == "__main__":
    main()
