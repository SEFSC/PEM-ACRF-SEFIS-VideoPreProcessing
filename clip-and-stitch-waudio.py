"""
GoPro Clip-and-Stitch Utility
-----------------------------
A frame-accurate video processing tool designed for compiling survey videos
from the Southeast Fishery Independent Survey (SEFIS). This script automates
the extraction and concatenation of specific video segments from GoPro camera
folders based on "time-on-bottom" timestamps provided in a CSV file. It
ensures seamless stitching of video segments with precise millisecond
alignment, even across GoPro chapter seams, while also handling NTSC timing.
It works identical to `clip-and-stitch.py` but includes the audio tracks in
the new video.

Key Features:
    * Frame-Accurate Seeking: Uses FFmpeg's `trim` and `setpts` filters to 
        ensure exact millisecond alignment across GoPro chapter seams.
    * NTSC Correction: Automatically handles 29.97 fps (30000/1001) 
        timing to prevent timecode drift in long deployments.
    * Diagnostic Overlays: Provides optional burned-in timecode with 
        `HH:MM:SS:FF` format for frame-by-frame verification.
    * GoPro Logic: Intelligently sorts chapters (GX01, GX02) to maintain 
        chronological continuity.

Usage:
    python clip-and-stitch-waudio.py path/to/name-of-configuration-file.yml

Required Dependencies:
    * pandas: For CSV data management.
    * yaml: For configuration parsing.
    * tqdm: For progress visualization.
    * FFmpeg/ffprobe: Must be installed and accessible via system path 
        or config file.

Author:  matt.grossi@noaa.gov with creation and refactoring assistance from
         Google Gemini Coding Partner
Project: Southeast Fishery Independent Survey (SEFIS)
Version: 2026.0.1
Note:    Gemini Coding Partner was used to assist with developing this code. The
         code has been reviewed, edited, validated, and documented by NOAA
         Fisheries staff.
"""

from datetime import datetime
import pandas as pd
import shutil
import yaml
import json
import os
import re
import sys
import time
import argparse
import subprocess
import textwrap
from tqdm import tqdm

def parse_args():
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(description="GoPro Clip-and-Stitch Utility")
    parser.add_argument(
        "config_path",
        type=str,
        nargs="?",
        default="configurations.yml",
        help="Path to the YAML configuration file (default: configurations.yml)"
    )
    return parser.parse_args()

def load_config(config_path='configurations.yml'):
    """Loads the YAML configuration file."""
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def get_video_metadata(file_path, ffprobe_path):
    """Uses ffprobe to get internal metadata of a video file.
    
    Arguments
    ---------
    file_path: str, path to the video file from which to extract metadata
    ffprobe_path: str, path to the `ffprobe` executable file`

    Returns
    -------
    list: [duration, creation_time, fps]
        duration: float, duration of the video in seconds
        creation_time: str, creation time of the video
            (e.g., "2024-01-01T12:00:00Z")
        fps: float, frames per second of the video
        bit_rate: int, bit rate of the video
        width: int, width of the video in pixels
        height: int, height of the video in pixels
    """
    # ffprobe Command
    #   -v: verbose level (quiet suppresses output)
    #   -print_format: output format (json for easy parsing)
    #   -show_format: show container format info (includes duration)
    #   -show_streams: show stream info (video, audio, etc.)
    # See https://ffmpeg.org/ffmpeg.html
    cmd = [
        ffprobe_path, "-v", "quiet", "-print_format", "json",
        "-show_format", "-show_streams", file_path
    ]
    result = subprocess.run(cmd, capture_output=True, text=True)
    data = json.loads(result.stdout)
    
    # Extract duration and creation time
    duration = float(data['format']['duration'])
    # GoPro creation_time is usually in format tags
    creation_time = data['format'].get('tags', {})\
                                  .get('creation_time', '1970-01-01T00:00:00Z')
    # Get bit rate to calculate file size (bytes) if needed for logging or QC
    bit_rate = int(data['format']['bit_rate'])
    
    # Extract width and height from video stream
    width = 0
    height = 0
    for stream in data.get('streams', []):
        if stream.get('codec_type') == 'video':
            width = int(stream.get('width'))
            height = int(stream.get('height'))
            break
    
    # Extract FPS from the video stream metadata
    fps = get_fps_from_metadata(metadata=data)

    return duration, creation_time, fps, bit_rate, width, height

def calculate_file_size(duration, bit_rate):
    """Calculates the file size in bytes based on duration and bit rate."""
    return int((duration * bit_rate) / 8)

def log_and_print(message, log_path, indent="    "):
    """Dents, indents, and writes a message to both console and log."""
    clean_msg = textwrap.indent(textwrap.dedent(message).strip(), indent)
    print(clean_msg + "\n", flush=True)
    with open(log_path, "a") as log:
        log.write(clean_msg + "\n\n")

def get_fps_from_metadata(metadata):
    """
    Parses the ffprobe JSON to find the video stream's average frame rate.
    """
    # Load the JSON if it's a string, or use the dict directly
    data = json.loads(metadata) if isinstance(metadata, str) else metadata
    
    for stream in data.get('streams', []):
        # Look specifically for the video stream
        if stream.get('codec_type') == 'video':
            fps_str = stream.get('avg_frame_rate')
            
            # avg_frame_rate is usually a fraction string like "30000/1001"
            if '/' in fps_str:
                num, den = map(int, fps_str.split('/'))
                return num / den
            return float(fps_str)
            
    return 29.97  # Fallback if no video stream is found

def get_ffmpeg_command(config, tool="ffmpeg"):
    """
    Finds ffmpeg or ffprobe. Checks local folder, then config, then system.
    Compatible with Windows (.exe) and Mac/Linux (no extension).
    """
    # Detect the file extension based on the OS
    extension = ".exe" if sys.platform.startswith("win") else ""
    executable_name = f"{tool}{extension}"

    # 1. Check local folder created by setup
    local_path = os.path.join(os.getcwd(), "ffmpeg", "bin", executable_name)
    if os.path.exists(local_path):
        return local_path
        
    # 2. Fallback to config file
    config_key = f"{tool}_path"
    config_val = config.get(config_key)
    if config_val and os.path.exists(config_val):
        return config_val
        
    # 3. Last resort: assume it is in the system PATH
    return tool

def time_ceiling(time_str: str) -> str:
    """Round time stamp up to the nearest 30 seconds"""
    # Strip frame from the string
    time_split, _, frame = time_str.rpartition(':')

    # Use pandas to round up the time to the nearest 30 seconds
    new_time = pd.to_datetime(time_split, format="%H:%M:%S")
    if int(frame) > 0:
        new_time += pd.Timedelta(seconds=1)
    new_time = new_time.ceil('30s')
    
    # Revert to str and append frame
    new_time_str = new_time.strftime("%H:%M:%S") + ":00"
    
    return new_time_str

def timestamp_to_seconds(ts_str, fps):
    """Converts HH:MM:SS:FF (frames) to total seconds (float)."""
    parts = ts_str.split(':')
    h, m, s, f = map(int, parts)
    return h * 3600 + m * 60 + s + (f / fps)

def seconds_to_timestamp(seconds, fps):
    """Converts total seconds (float) back to HH:MM:SS:FF format."""
    h = int(seconds // 3600)
    m = int((seconds % 3600) // 60)
    s = int(seconds % 60)
    # Convert the decimal remainder of the second into frame units
    f = int(round((seconds % 1) * fps))
    
    # If rounding pushes frames to 30, roll it over to the next second
    if f >= int(round(fps)):
        f = 0
        s += 1
        # (Add logic here to roll over minutes/hours if needed for absolute perfection)
        
    return f"{h:02}:{m:02}:{s:02}:{f:02}"

def get_gopro_sort_key(filename):
    """
    Parses GoPro filenames (e.g., GX010192.MP4) for correct sorting.
    Returns a tuple: (Recording ID, Chapter Number)
    Example: GX020192 -> (0192, 02)
    """
    match = re.search(r'([A-Z]{2})(\d{2})(\d{4})', filename.upper())
    if match:
        prefix, chapter, rec_id = match.groups()
        return (int(rec_id), int(chapter))
    return (0, 0)

def process_deployments(config_path='configurations.yml'):
    # Strip quotes that Windows adds when dragging/dropping files
    config_path = config_path.strip('"')
    
    # 1. SETUP & VALIDATION
    config = load_config(config_path)
    os.makedirs(config['output_directory'], exist_ok=True)
    
    # Get FFmpeg paths
    ffmpeg_exe = get_ffmpeg_command(config, "ffmpeg")
    ffprobe_exe = get_ffmpeg_command(config, "ffprobe")

    # SETTINGS THAT CAN ALSO BE OVERRIDDEN IN THE YAML CONFIG FILE
    config['clear_log'] = config.get('clear_log', False)
    config['diagnostic_mode'] = config.get('diagnostic_mode', False)
    config['log_file'] = config.get('log_file', 'processing_log.txt')
    config['preread_time_min'] = config.get('preread_time_min', 8)
    config['reprocess'] = config.get('reprocess', False)
    config['timeout_min'] = config.get('timeout_min', None)
    config['use_gpu'] = config.get('use_gpu', False)
    config['video_duration_min'] = config.get('video_duration_min', 24)
    config['video_extension'] = config.get('video_extension', '.MP4')
    
    # Convert times to seconds
    timeout = int(config['timeout_min']) * 60 if config.get('timeout_min') else None
    preread_time_sec = int(config['preread_time_min']) * 60
    video_duration_sec = int(config['video_duration_min']) * 60

    # Video encoding quality. Lower values mean better quality:
    # Defaults to "auto" whereby the original video's bit rate is used to
    # dynamically calculate a target bitrate for the output video. 
    config['quality_crf'] = config.get('quality_crf', 'auto')
    is_auto_mode = str(config['quality_crf']).lower() == 'auto'

    # Minimum disk space required to run script (in GB). Script will warn if
    # available space is below this threshold.
    config['min_gb_required'] = config.get('min_gb_required', 10)

    # Check Disk Space
    total, used, free = shutil.disk_usage(config['output_directory'])
    if free // (2**30) < config['min_gb_required']:
        log_and_print(f"WARNING: Low disk space ({free // (2**30)}GB remaining).", config['log_file'])

    # Initialize the session in the log file, wiping it clean if desired.
    mode = "w" if config['clear_log'] else "a"
    with open(config['log_file'], mode) as log:
        log.write(f"{'#'*80}\n")
        log.write(f"SESSION START: {datetime.now()}\n")
        log.write(f"CONFIGURATION: {config_path}\n\n")
        for key, value in config.items():
            log.write(f"  -> {key}: {value}\n")
        log.write("\n")
        log.write(f"{'#'*80}\n\n")

    # Load CSV with encoding fallback
    try:
        df = pd.read_csv(config['csv_path'], encoding='utf-8')
    except UnicodeDecodeError:
        df = pd.read_csv(config['csv_path'], encoding='ISO-8859-1')
    if len(df) == 0:
        print("ERROR: CSV file appears to be empty.")
        return

    # Clean and append to CSV
    df.columns = df.columns.str.strip()
    df[config['col_foldername']] = df[config['col_foldername']].str.strip()
    df['timebottom_ceil'] = df[config['col_timebottom']].apply(time_ceiling)    

    # Check for duplicates in foldername
    if df[config['col_foldername']].duplicated().any():
        print("ERROR: Duplicate folder names found in CSV. Aborting.")
        return

    # 2. ITERATE THROUGH EACH VIDEO FOLDER
    for _, row in tqdm(df.iterrows(), total=len(df), desc="Total Progress"):
        # Start timer for this video
        iter_start = time.perf_counter()

        folder_id = str(row[config['col_foldername']]).strip()
        time_bottom_ceil = str(row['timebottom_ceil']).strip()

        # Input and output directories
        folder_path = os.path.join(config['input_directory'], folder_id)
        if not os.path.exists(folder_path):
            with open(config['log_file'], "a") as log:
                log.write(f"SKIP: Folder {folder_path} not found.\n")
            continue
        output_path = os.path.join(config['output_directory'],
                                   f"{folder_id}{config['video_extension']}")

        # Skip if output exists and reprocess is false
        if not config['reprocess'] and os.path.exists(output_path):
            with open(config['log_file'], "a") as log:
                log.write(f"{os.path.basename(output_path)} exists and reprocess is set to False. Nothing to process. Skipping.\n")
            continue

        # Skip and log any folder listed in the CSV that doesn't exist in the
        # input directory
        if not os.path.exists(folder_path):
            with open(config['log_file'], "a") as log:
                log.write(f"SKIP: Folder {folder_id} not found.\n")
            continue

        # 3. GATHER & SORT FILES
        video_files = [f for f in os.listdir(folder_path) 
                       if f.upper().endswith(config['video_extension'].upper())]
        
        # Skip and log if no video files are found in the folder
        if not video_files:
            with open(config['log_file'], "a") as log:
                log.write(f"SKIP: No videos in {folder_id}.\n")
            continue

        # Sort by GoPro filename instead of metadata creation_time, since all
        # file chunks have the same creation time
        video_files.sort(key=get_gopro_sort_key)

        # Get file duration from the metadata
        print(f"\n  > Probing metadata for {len(video_files)} videos in {folder_id}...", flush=True)
        file_data = []
        for f in video_files:
            full_p = os.path.join(folder_path, f)
            dur, _, fps, br, w, h = get_video_metadata(
                file_path=full_p,
                ffprobe_path=ffprobe_exe
                )
            file_data.append({'path': full_p, 'duration': dur, 'fps': fps, 'bit_rate': br, 'width': w, 'height': h})

        # 4. CALCULATE TIMELINE
        # Start time is `preread_time_min` minutes (default 8) from the time on
        # the bottom rounded up to the nearest 30 seconds
        # End time is `video_duration_min` minutes (default 24) after the start
        # time
        start_seconds = timestamp_to_seconds(time_bottom_ceil, fps) + preread_time_sec
        end_seconds = start_seconds + video_duration_sec
        
        # Check the start times and durations of each video to determine which
        # files are needed to stitch together and where to clip partial videos
        print("  > Determining needed files and trim points...", flush=True)
        cumulative_time = 0
        needed_files = []
        for data in file_data:
            file_start = cumulative_time
            file_end = cumulative_time + data['duration']
            
            # Check whether this file overlaps with the desired clip range
            if file_end > start_seconds and file_start < end_seconds:
                # Store the relative start/end for THIS specific file
                rel_start = max(0, start_seconds - file_start)
                rel_end = min(data['duration'], end_seconds - file_start)

                # Calculate BPP for the current chapter
                bpp = data['bit_rate'] / (data['width'] * data['height'] * data['fps'])
                
                needed_files.append({
                    'path': data['path'], 
                    'ss': rel_start, 
                    't': rel_end - rel_start,
                    'size': calculate_file_size(rel_end - rel_start, data['bit_rate']),
                    'bpp': bpp,
                    'width': data['width'],
                    'height': data['height'],
                    'fps': data['fps'],
                    'bit_rate': data['bit_rate']
                })
            cumulative_time = file_end

        # If no footage matches the 24-minute window, log and skip
        if not needed_files:
            with open(config['log_file'], "a") as log:
                log.write(f"SKIP: {folder_id} - No footage found for the requested time window.\n")
            continue

        # 5. FFmpeg COMMAND
        # Trim each file INDIVIDUALLY before stitching:
        cumulative_size = 0
        cumulative_bpp = 0
        input_args = []
        filter_complex_parts = []
        filter_inputs = ""
        for i, f_info in enumerate(needed_files):
            # Update cumulative statistics
            cumulative_size += f_info['size']
            cumulative_bpp += f_info['bpp']

            # Add the input file normally
            input_args.extend(["-i", f_info['path']])

            # Add a small 'epsilon' to the duration to ensure the final frame
            # is included in the trim.
            trim_duration = f_info['t'] + 0.1

            # Trim BOTH video (v) and audio (a) segments and reset their 
            # clocks (setpts/asetpts) to 0 so the stitcher sees a clean 
            # sequence starting from 0.0s
            v_label = f"[v{i}]"
            a_label = f"[a{i}]"
            filter_complex_parts.append(
                f"[{i}:v]trim=start={f_info['ss']}:duration={trim_duration},"
                f"setpts=PTS-STARTPTS{v_label};"
                f"[{i}:a]atrim=start={f_info['ss']}:duration={trim_duration},"
                f"asetpts=PTS-STARTPTS{a_label}"
            )
            # Concat requires inputs in [v0][a0][v1][a1]... order
            filter_inputs += f"{v_label}{a_label}"

        # Target bitrate based on original GoPro metadata to ensure visual fidelity
        if is_auto_mode:
            target_bitrate = f"{int(cumulative_size * 8 / video_duration_sec)}"
            print(f"  > Targeting bitrate {int(target_bitrate)/1_000_000:.2f} Mbps to match source density.", flush=True)

        # Build the filter string: e.g., `[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v_stitched][outa]`:
        concat_part = f"{filter_inputs}concat=n={len(needed_files)}:v=1:a=1[v_stitched][outa]"
        filter_complex_parts.append(concat_part)

        # Force the frame rate (fps=fps={fps}) right after the concat to 
        # prevent NTSC drift during the stitch. 'round=near' prevents frame
        # drops due to NTSC math drift.
        filter_complex_parts.append(f"[v_stitched]fps=fps={fps}:round=near[outv]")
        
        # Get fonts to prevent potential crashes on Windows
        if config.get('diagnostic_mode'):
            # Safeguard against missing fonts on Windows systems with OS-specific font selections
            if sys.platform.startswith("win"):
                font_path = r"C\:/Windows/Fonts/arial.ttf"
            elif sys.platform == "darwin":
                font_path = "/Library/Fonts/Arial.ttf"
            else:
                font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

            check_path = font_path.replace(r"C\:", "C:").replace("\\", "")
            if not os.path.exists(check_path) and not sys.platform.startswith("win"):
                 print("WARNING: Default font not found. Diagnostic text may fail.")

            # Add a diagnostic timestamp overlay in the top-left corner
            drawtext_filter = (
                f"[outv]drawtext=fontfile='{font_path}':"
                r"text='%{eif\:t/3600\:d\:2}\:%{eif\:mod(t/60,60)\:d\:2}\:%{eif\:mod(t,60)\:d\:2}\:%{eif\:" + str(fps) + r"*mod(t,1)\:d\:2}':"
                "x=10:y=10:fontsize=48:fontcolor=white:box=1:boxcolor=black@0.5[diagout]"
            )
            filter_complex_parts.append(drawtext_filter)
            maparg_v = "[diagout]"
        else:
            maparg_v = "[outv]"

        # Join all filter parts with semicolons
        filter_str = "; ".join(filter_complex_parts)

        # 6. GENERATE QC TABLE
        cumulative = 0
        table_lines = []

        # Header
        table_lines.append(f"\n{'='*80}")
        table_lines.append(f"{'QC SEAM INSPECTION TABLE - Folder: ' + folder_id + ' (' + str(config['video_duration_min']) + ' min)':^80}")
        table_lines.append(f"{'='*80}")
        table_lines.append(f"{'NEW VIDEO TIME':<18} | {'ACTION':<17} | {'SOURCE FILE':<17} | {'SOURCE TIMESTAMP'}")
        table_lines.append(f"{'-'*19}|{'-'*19}|{'-'*19}|{'-'*20}")

        # Content
        for i, segment in enumerate(needed_files):
            file_name = os.path.basename(segment['path'])
            start_ts = seconds_to_timestamp(cumulative, fps)
            source_start = seconds_to_timestamp(segment['ss'], fps)
            
            table_lines.append(f"{start_ts:<18} | START SEGMENT     | {file_name:<17} | {source_start}")
            
            cumulative += segment['t']
            end_ts = seconds_to_timestamp(cumulative, fps)
            source_end = seconds_to_timestamp(segment['ss'] + segment['t'], fps)
            
            if i < len(needed_files) - 1:
                table_lines.append(f"{end_ts:<18} | SEAM / STITCH     | {file_name:<17} | {source_end}")
                table_lines.append(f"{' '*18} |        |||        | {' '*17} |")
                table_lines.append(f"{' '*18} |        vvv        | {' '*17} |")
            else:
                table_lines.append(f"{end_ts:<18} | VIDEO END         | {file_name:<17} | {source_end}")

        table_lines.append(f"{'='*80}\n")
        full_table_str = "\n".join(table_lines)

        # Write the table to the log file
        with open(config['log_file'], "a") as log:
            log.write(full_table_str)

        # Only print to the console if diagnostic mode is on
        if config.get('diagnostic_mode'):
            print(full_table_str)

        # Build execution command
        if config['use_gpu']:
            encoder_args = [
                "-c:v", "h264_nvenc",
                "-rc", "vbr",
            ]
            if is_auto_mode:
                encoder_args += ["-b:v", target_bitrate, "-maxrate", "100M", "-bufsize", "100M"]
            else:
                encoder_args += ["-b:v", "0", "-cq", str(config['quality_crf'])]
            encoder_args += ["-preset", "p7"]
        else:
            encoder_args = [
                "-c:v", "libx264",
            ]
            if is_auto_mode:
                encoder_args += ["-b:v", target_bitrate]
            else:
                encoder_args += ["-crf", str(config['quality_crf'])]
            encoder_args += ["-preset", "medium"]
        
        cmd = [
            ffmpeg_exe, "-y"
        ] + input_args + [
            "-filter_complex", filter_str,
            "-map", maparg_v,
            "-map", "[outa]",
        ] + encoder_args + [
            "-c:a", "aac",
            "-t", str(video_duration_sec),  # Dynamic safety ceiling
            output_path
        ]

        # Run the command and log any errors
        print("  > Clipping and stitching... This may take some time.\n", flush=True)
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"\nERROR: ffmpeg timed out on {folder_id}. Is the network drive disconnected?")
            continue
            
        if result.returncode != 0:
            with open(config['log_file'], "a") as log:
                log.write(f"ERROR in {folder_id}: {result.stderr}\n")
        
        # Calculate actual metrics for the final video
        if os.path.exists(output_path):
            actual_size = os.path.getsize(output_path)
            actual_bitrate = (actual_size * 8) / video_duration_sec
            actual_bpp = actual_bitrate / (needed_files[0]['width'] * needed_files[0]['height'] * needed_files[0]['fps'])
        else:
            actual_size = 0
            actual_bpp = 0

        # Calculate and print elapsed time with final QC metrics
        iter_duration = time.perf_counter() - iter_start
        avg_bpp_src = cumulative_bpp / len(needed_files)
        expectations_table_lines = []
        expectations_table_lines.append(f"{' '*23} | EXPECTED {' '*7} ACTUAL")
        expectations_table_lines.append(f"    {'-'*20}|{'-'*30}")
        expectations_table_lines.append(f"    OUTPUT FILE SIZE    | {cumulative_size / (2**30):.2f} GB {' ':<4} --> {actual_size / (2**30):.2f} GB")
        expectations_table_lines.append(f"    BITRATE             | {int(target_bitrate)/1_000_000:.2f} Mbps {' ':<1} --> {actual_bitrate/1_000_000:.2f} Mbps")
        expectations_table_lines.append(f"    INFORMATION DENSITY | {avg_bpp_src:.4f} BPP {' ':<1} --> {actual_bpp:.4f} BPP")
        expectations_table_str = "\n".join(expectations_table_lines) +"\n"
        
        if iter_duration > 60:
            print(f"  > Created {f"{folder_id}{config['video_extension']}"} in {iter_duration/60:.2f} minutes.", flush=True)
        else:
            print(f"  > Created {f"{folder_id}{config['video_extension']}"} in {iter_duration:.2f} seconds.", flush=True)
        print("  > Output file statistics vs. expectations:\n", flush=True)
        print(expectations_table_str, flush=True)

        # Check expectations
        is_bpp_ideal_80 = avg_bpp_src * 0.80 <= actual_bpp <= avg_bpp_src * 1.20
        is_size_ideal_80 = cumulative_size * 0.80 <= actual_size <= cumulative_size * 1.20
        is_bpp_ideal_90 = avg_bpp_src * 0.90 <= actual_bpp <= avg_bpp_src * 1.10
        is_size_ideal_90 = cumulative_size * 0.90 <= actual_size <= cumulative_size * 1.10

        # Add to log file
        with open(config['log_file'], "a") as log:
            log.write(f"SUMMARY OF FOLDER {folder_id}:\n")
            log.write(f"    Estimated output video size without visual quality loss: {cumulative_size / (2**30):.2f} GB\n")
            log.write(f"    Estimated target bitrate to maintain visual fidelity: {int(target_bitrate)/1_000_000:.2f} Mbps\n")
            log.write(f"    Average information density of original videos: {avg_bpp_src:.4f} bits per pixel (BPP)\n\n")
            log.write(' '*30 + '* '*10 + '\n\n')
            log.write(f"    Output video file size: {actual_size / (2**30):.2f} GB\n")
            log.write(f"    Output video bitrate:   {actual_bitrate/1_000_000:.2f} Mbps\n")
            log.write(f"    Information density:    {actual_bpp:.4f} BPP\n\n")
            log.write(f"    -> Within 80% of original average BPP:  {'YES' if is_bpp_ideal_80 else 'NO  X'}\n")
            log.write(f"    -> Within 80% of estimated file size:   {'YES' if is_size_ideal_80 else 'NO  X'}\n")
            log.write(f"    -> Within 90% of original average BPP:  {'YES' if is_bpp_ideal_90 else 'NO  X'}\n")
            log.write(f"    -> Within 90% of estimated file size:   {'YES' if is_size_ideal_90 else 'NO  X'}\n\n")

        # Centralized multi-line notes and warnings
        quality_note = """
            NOTE: This utility uses a `libx264` (CPU) encoder that is generally more
            efficient than the GoPro's internal hardware. This means you will often
            find that an output file with a lower bitrate than the original actually
            contains the same amount of visual information. Your target shouldn't
            necessarily be an identical file size, but rather a file size that stays
            within 80-90% of the original and a BPP within 80% of the original in
            order to maintain "visually lossless" quality.
        """
        
        warning_90 = """
            WARNING: Output video is NOT within 90% of the original BPP and/or file
                     size. Output may be too small (quality loss) or too large (wasted
                     space).
        """

        warning_80 = """
            WARNING: Output video is NOT within 80% of the original BPP and/or file
                     size. Output may be too small (quality loss) or too large (wasted
                     space).
        """

        if not all([is_bpp_ideal_90, is_size_ideal_90]):
            log_and_print(warning_90, config['log_file'])
            log_and_print(quality_note, config['log_file'])
        elif not all([is_bpp_ideal_80, is_size_ideal_80]):
            log_and_print(warning_80, config['log_file'])
            log_and_print(quality_note, config['log_file'])

    # Add space to end of log file for readability between runs
    with open(config['log_file'], "a") as log:
        log.write("\n\n\n")

if __name__ == "__main__":
    # Parse command-line arguments
    args = parse_args()

    # Launch the script
    process_deployments(config_path=args.config_path)
    print("Processing Complete. Check processing_log.txt for details.")