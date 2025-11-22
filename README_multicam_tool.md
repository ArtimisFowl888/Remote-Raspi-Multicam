# Multicam Processing Tool

This tool processes video takes recorded by the Pi Multicam system for use in Adobe Premiere Pro.

## Features
1.  **XMP Sidecars**: Generates `.xmp` sidecar files for each video clip. This ensures markers appear on the clips when imported into Premiere.
2.  **Multicam XML**: Generates a `.xml` file (Final Cut Pro 7 XML) for each take. Importing this into Premiere creates a synchronized sequence with all camera angles and sequence markers.

## Requirements
*   **Python 3**: Must be installed and in your system PATH.
*   **FFmpeg/FFprobe**: Recommended for accurate video duration detection. If not found, the tool defaults to 10 seconds per clip (which you can extend in Premiere).
    *   Download from [ffmpeg.org](https://ffmpeg.org/download.html) and add to your PATH.

## Usage

1.  Open a terminal (Command Prompt or PowerShell).
2.  Navigate to the folder containing the script.
3.  Run the script pointing to your downloads folder:

```bash
python process_takes.py "C:\path\to\video_downloads"
```

### Options

*   `--burn`: Hard-code the text overlay into the video files. Requires FFmpeg.
*   `--position [bottom|top|top-left|...]`: Set the position of the burned text. Default is `bottom`.

Example:
```bash
python process_takes.py Test_videos --burn --position top-left
```

## Output
*   **Sidecar Files**: `filename.xmp` created next to each video file.
*   **Subtitle Files**: `filename.srt` created next to each video file (contains telemetry overlay).
*   **Burned Videos**: `filename_burned.mp4` (if `--burn` is used).
*   **XML Sequence**: `take_name.xml` created in each take folder.

## Importing into Premiere Pro
1.  **Drag and Drop**: You can drag the generated `.xml` file directly into the Premiere Pro Project panel.
2.  **Sequence**: A new sequence will be created. Open it to see your synced multicam clips.
3.  **Markers**: Markers from the recording will appear on the timeline.
