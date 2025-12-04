import sys
import os
import subprocess
import json

def get_info(file_path):
    cmd = [
        "ffprobe", "-v", "error", "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,avg_frame_rate,duration",
        "-of", "json", file_path
    ]
    try:
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        return json.loads(result.stdout)
    except Exception as e:
        return str(e)

file1 = r'c:\Users\zyap\Documents\My_apps\pi_multicam\Remote-Raspi-Multicam\Test_videos\test2\192.168.0.101\test2_192_168_0_101_20251124_161113_000000.mp4'
file2 = r'c:\Users\zyap\Documents\My_apps\pi_multicam\Remote-Raspi-Multicam\Test_videos\test2\192.168.0.103\test2_192_168_0_103_20251124_161113_000000.mp4'

output_file = r'c:\Users\zyap\Documents\My_apps\pi_multicam\Remote-Raspi-Multicam\output_test2.txt'
with open(output_file, 'w') as f:
    f.write(f"File 1: {file1}\n")
    f.write(str(get_info(file1)) + "\n")
    f.write("-" * 20 + "\n")
    f.write(f"File 2: {file2}\n")
    f.write(str(get_info(file2)) + "\n")

print(f"Output written to {output_file}")
