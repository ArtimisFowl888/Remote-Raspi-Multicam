import os
import sys
import glob
import re
import datetime
import argparse
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
    Estimates video duration. 
    Since we don't want external dependencies like ffprobe if possible, 
    and we know the segment time from the project (default 20 mins),
    we might need a way to get this. 
    
    However, for accurate XML, we really need the duration.
    For now, I will try to use 'ffprobe' if available, otherwise default to a placeholder.
    """
    import subprocess
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=noprint_wrappers=1:nokey=1", file_path],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True
        )
        return float(result.stdout.strip())
    except Exception:
        print(f"Warning: Could not get duration for {file_path} (ffprobe not found?). Using default 10s.")
        return 10.0

def generate_xmp(video_path, markers, start_time_dt):
    """
    Generates an XMP sidecar file for the given video.
    markers: list of (datetime, note)
    start_time_dt: datetime of the video start
    """
    xmp_path = os.path.splitext(video_path)[0] + ".xmp"
    
    # XMP Template
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
        # Calculate offset in seconds
        delta = (mark_time - start_time_dt).total_seconds()
        if delta < 0:
            continue # Marker is before this clip
            
        # We need to know if the marker is *within* this clip.
        # But for sidecars, usually we just put all markers relative to start?
        # Premiere is smart enough to ignore ones out of range, usually.
        # But better if we check duration if possible. 
        # For now, we just add them if they are positive relative to start.
        
        # Format: 254016000000 is a common base for NTSC/PAL mix, but let's just use seconds * 254016000000 ?
        # Actually, simpler is to use standard timecode or frame count if we knew framerate.
        # Let's try to use the simple structure Adobe uses.
        
        # Adobe XMP markers use a specific tick rate. 
        # Often 254016000000 per second.
        start_ticks = int(delta * 254016000000)
        duration_ticks = 0 # Point marker
        
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

def generate_xml(take_name, take_dir, clips_by_ip, all_markers):
    """
    Generates FCP7 XML for the take.
    clips_by_ip: dict { ip: [ {path, start_time, duration} ] }
    all_markers: list of (datetime, note)
    """
    
    # Find the earliest start time to be the sequence start
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
    ET.SubElement(sequence, "duration").text = "10000" # Placeholder
    
    rate = ET.SubElement(sequence, "rate")
    ET.SubElement(rate, "timebase").text = "25"
    ET.SubElement(rate, "ntsc").text = "FALSE"
    
    media = ET.SubElement(sequence, "media")
    video = ET.SubElement(media, "video")
    
    # Format definition
    format_node = ET.SubElement(video, "format")
    samplecharacteristics = ET.SubElement(format_node, "samplecharacteristics")
    rate_sc = ET.SubElement(samplecharacteristics, "rate")
    ET.SubElement(rate_sc, "timebase").text = "25"
    ET.SubElement(samplecharacteristics, "width").text = "1920"
    ET.SubElement(samplecharacteristics, "height").text = "1080"
    ET.SubElement(samplecharacteristics, "pixelaspectratio").text = "square"

    # Tracks
    # We create one track per IP (camera angle)
    sorted_ips = sorted(clips_by_ip.keys())
    
    for i, ip in enumerate(sorted_ips):
        track = ET.SubElement(video, "track")
        
        clips = sorted(clips_by_ip[ip], key=lambda x: x['start_time'])
        
        for clip in clips:
            # Calculate start frame on timeline
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
            
            # File reference
            file_node = ET.SubElement(clipitem, "file", id=f"file-{os.path.basename(clip['path'])}")
            ET.SubElement(file_node, "name").text = os.path.basename(clip['path'])
            ET.SubElement(file_node, "pathurl").text = "file://localhost/" + clip['path'].replace("\\", "/")
            
            rate_f = ET.SubElement(file_node, "rate")
            ET.SubElement(rate_f, "timebase").text = "25"
            
            media_f = ET.SubElement(file_node, "media")
            video_f = ET.SubElement(media_f, "video")
            ET.SubElement(video_f, "duration").text = str(duration_frames)

    # Sequence Markers
    if all_markers:
        markers_node = ET.SubElement(sequence, "marker")
        # Wait, FCP7 XML markers are inside <marker> tags, usually under <sequence> or <clipitem>
        # But for sequence markers, it's a list of <marker> elements.
        
        for mark_time, note in all_markers:
            offset_seconds = (mark_time - global_start).total_seconds()
            if offset_seconds >= 0:
                frame = int(offset_seconds * 25)
                
                marker = ET.SubElement(sequence, "marker")
                ET.SubElement(marker, "name").text = note if note else "Marker"
                ET.SubElement(marker, "in").text = str(frame)
                ET.SubElement(marker, "out").text = str(frame) # Duration 0 for point marker
                ET.SubElement(marker, "comment").text = note if note else ""

    # Save XML
    xml_str = minidom.parseString(ET.tostring(root)).toprettyxml(indent="  ")
    xml_path = os.path.join(take_dir, f"{take_name}.xml")
    with open(xml_path, "w", encoding="utf-8") as f:
        f.write(xml_str)
    print(f"Generated XML: {xml_path}")

def process_take(take_path):
    take_name = os.path.basename(take_path)
    print(f"Processing take: {take_name}")
    
    # Data structure to hold clips
    clips_by_ip = {}
    all_markers = []
    
    # Walk through IP folders
    for ip_folder in os.listdir(take_path):
        ip_path = os.path.join(take_path, ip_folder)
        if not os.path.isdir(ip_path):
            continue
            
        # Find markers file
        marker_files = glob.glob(os.path.join(ip_path, "*_markers.txt"))
        take_markers = []
        if marker_files:
            with open(marker_files[0], "r") as f:
                for line in f:
                    if line.startswith("MARK:"):
                        # Parse MARK: <ISO> | <NOTE>
                        parts = line.strip().split("MARK:", 1)[1].strip().split("|", 1)
                        ts_str = parts[0].strip()
                        note = parts[1].strip() if len(parts) > 1 else ""
                        dt = parse_timestamp(ts_str)
                        if dt:
                            take_markers.append((dt, note))
                            all_markers.append((dt, note))
        
        # Find video files
        video_files = glob.glob(os.path.join(ip_path, "*.mp4"))
        if not video_files:
             video_files = glob.glob(os.path.join(ip_path, "*.h264"))
             
        if not video_files:
            continue
            
        clips_by_ip[ip_folder] = []
        
        for vid in video_files:
            # Parse filename: take_safeip_YYYYMMDD_HHMMSS_segment.mp4
            # Regex to find timestamp
            match = re.search(r"_(\d{8}_\d{6})_", os.path.basename(vid))
            if match:
                ts_str = match.group(1)
                start_time = parse_timestamp(ts_str)
                if start_time:
                    duration = get_video_duration(vid)
                    
                    # Add segment offset if needed? 
                    # Actually the filename timestamp is likely the start of that segment 
                    # OR start of recording. 
                    # If it's start of recording, we need segment index.
                    # Filename: ..._000000.mp4
                    seg_match = re.search(r"_(\d{6})\.(mp4|h264)$", os.path.basename(vid))
                    if seg_match:
                        seg_idx = int(seg_match.group(1))
                        # If timestamp is constant for all segments (start of take), add offset
                        # But usually rotating file loggers update timestamp?
                        # Let's check the code. 
                        # control_pi_webapp.py line 400: base_filename = f"{take_name}_{safe_ip}_{timestamp}"
                        # video_filename_template = ... f"{base_filename}_%06d.mp4"
                        # So the timestamp in filename is the START of the recording.
                        # We need to add segment_index * segment_duration to get actual start.
                        # Default segment is 20 mins (1200s).
                        
                        # However, we don't know if user changed config.
                        # Better to trust the file creation time? No, that's unreliable.
                        # We should assume 20 mins or just rely on the fact that we place them sequentially on timeline?
                        # XML placement needs exact time.
                        
                        # Let's assume 1200s segment for now as per default in listener.
                        segment_duration = 1200.0 
                        start_time = start_time + datetime.timedelta(seconds=seg_idx * segment_duration)
                    
                    clips_by_ip[ip_folder].append({
                        'path': vid,
                        'start_time': start_time,
                        'duration': duration
                    })
                    
                    # Generate Sidecar
                    generate_xmp(vid, take_markers, start_time)

    # Generate XML
    generate_xml(take_name, take_path, clips_by_ip, all_markers)

def main():
    parser = argparse.ArgumentParser(description="Process multicam takes for Premiere Pro.")
    parser.add_argument("directory", help="Directory containing takes (e.g. video_downloads)")
    args = parser.parse_args()
    
    base_dir = args.directory
    if not os.path.isdir(base_dir):
        print(f"Error: {base_dir} is not a directory.")
        return

    # Check if this is a take directory itself (contains IPs) or a root dir
    # Heuristic: check if subdirs look like IPs or if they contain IPs
    # Actually, let's just assume it's the root 'video_downloads' folder containing takes.
    
    for item in os.listdir(base_dir):
        path = os.path.join(base_dir, item)
        if os.path.isdir(path):
            # Check if it looks like a take (contains IP folders)
            # IP folder regex: \d+\.\d+\.\d+\.\d+ or similar
            has_ips = False
            for sub in os.listdir(path):
                if re.match(r"\d+\.\d+\.\d+\.\d+", sub) or re.match(r"\d+_\d+_\d+_\d+", sub):
                    has_ips = True
                    break
            
            if has_ips:
                process_take(path)

if __name__ == "__main__":
    main()
