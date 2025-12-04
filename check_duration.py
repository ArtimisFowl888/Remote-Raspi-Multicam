import sys
import os
sys.path.append(r'c:\Users\zyap\Documents\My_apps\pi_multicam\Remote-Raspi-Multicam')
from process_takes import get_video_duration

file1 = r'c:\Users\zyap\Documents\My_apps\pi_multicam\Remote-Raspi-Multicam\Test_videos\gusting_test_1\192.168.0.101\gusting_test_1_192_168_0_101_20251025_111137_000000.mp4'
file2 = r'c:\Users\zyap\Documents\My_apps\pi_multicam\Remote-Raspi-Multicam\Test_videos\gusting_test_1\192.168.0.102\gusting_test_1_192_168_0_102_20251025_111137_000000.mp4'

print(f"File 1: {file1}")
d1 = get_video_duration(file1)
print(f"Duration 1: {d1}")

print(f"File 2: {file2}")
d2 = get_video_duration(file2)
print(f"Duration 2: {d2}")
