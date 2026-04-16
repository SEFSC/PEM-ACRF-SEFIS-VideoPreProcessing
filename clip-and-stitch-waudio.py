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
    """Uses ffprobe to get internal creation time and duration of a video file.
    
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
    
    # Extract FPS from the video stream metadata
    fps = get_fps_from_metadata(metadata=data)

    return duration, creation_time, fps

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
    config['video_extension'] = config.get('video_extension', '.MP4')
    config['reprocess'] = config.get('reprocess', False)
    config['timeout_min'] = config.get('timeout_min', None)
    config['use_gpu'] = config.get('use_gpu', False)
    
    # Convert timeout to seconds
    timeout = config['timeout_min'] * 60 if config.get('timeout_min') else None

    # Video encoding quality. Lower values mean better quality:
    #   -> 18 is high quality, 23 is standard
    #   -> 10 with `use_gpu: false` or 11 with `use_gpu: true` produced bit
    #      rate and file size most similar to those of the original GoPro files
    #      during trial and error testing. Often machine-dependent.
    config['quality_crf'] = config.get('quality_crf', 10)
    
    # Minimum disk space required to run script (in GB). Script will warn if
    # available space is below this threshold.
    config['min_gb_required'] = config.get('min_gb_required', 10)

    # Check Disk Space
    total, used, free = shutil.disk_usage(config['output_directory'])
    if free // (2**30) < config['min_gb_required']:
        print(f"WARNING: Low disk space ({free // (2**30)}GB remaining).")

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

    # Clean CSV: Strip whitespace from headers and folder names
    df.columns = df.columns.str.strip()

    # Check for duplicates in foldername
    if df[config['col_foldername']].duplicated().any():
        print("ERROR: Duplicate folder names found in CSV. Aborting.")
        return

    # 2. ITERATE THROUGH EACH VIDEO FOLDER
    for _, row in tqdm(df.iterrows(), total=len(df), desc="Total Progress"):
        # Start timer for this video
        iter_start = time.perf_counter()

        folder_id = str(row[config['col_foldername']]).strip()
        time_bottom_str = str(row[config['col_timebottom']]).strip()

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
            dur, _, fps = get_video_metadata(
                file_path=full_p,
                ffprobe_path=ffprobe_exe
                )
            file_data.append({'path': full_p, 'duration': dur, 'fps': fps})

        # 4. CALCULATE TIMELINE
        # Start time is 8 minutes (480 seconds) from the time on the bottom,
        # end time is 24 minutes (1440 seconds) after the start time
        start_seconds = timestamp_to_seconds(time_bottom_str, fps) + 480
        end_seconds = start_seconds + 1440
        
        # Check the start times and durations of each video to determine which
        # files are needed to stitch together and where to clip partial videos
        print(f"  > Determining needed files and trim points...", flush=True)
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
                needed_files.append({
                    'path': data['path'], 
                    'ss': rel_start, 
                    't': rel_end - rel_start
                })
            cumulative_time = file_end

        # If no footage matches the 24-minute window, log and skip
        if not needed_files:
            with open(config['log_file'], "a") as log:
                log.write(f"SKIP: {folder_id} - No footage found for the requested time window.\n")
            continue

        # 5. FFmpeg COMMAND
        # Trim each file INDIVIDUALLY before stitching:
        #   -i: input media file
        #   -t: duration of the clip to take (24 mins = 1440 seconds)
        #   -ss: seeks to the start point for the specified file (relative to
        #        the start of the concatenated stream)
        # See https://ffmpeg.org/ffmpeg.html
        input_args = []
        filter_complex_parts = []
        filter_inputs = ""
        for i, f_info in enumerate(needed_files):
            # Add the input file normally
            input_args.extend(["-i", f_info['path']])

            # Add a small 'epsilon' to the duration to ensure the final frame
            # is included in the trim.
            trim_duration = f_info['t'] + 0.1

            # Trim the video (v) and audio (a) using start time and duration
            # (seconds) and reset the clock of the trimmed segment (setpts) to
            # 0 so the stitcher sees a clean sequence starting from 0.0s
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

        # Build the filter string: e.g., `[0:v][0:a][1:v][1:a]concat=n=2:v=1:a=1[v_stitched][outa]`:
        # Concatenate video (v) and audio (a) from files 0-1 into 1 video from
        # the 2 inputs
        concat_part = f"{filter_inputs}concat=n={len(needed_files)}:v=1:a=1[v_stitched][outa]"
        filter_complex_parts.append(concat_part)

        # Force the frame rate (fps=fps={fps}) right after the concat to 
        # prevent NTSC drift during the stitch. 'round=near' prevents frame
        # drops due to NTSC math drift.
        filter_complex_parts.append(f"[v_stitched]fps=fps={fps}:round=near[outv]")

        # Get fpmts to prevent potential crashes on Windows
        if config.get('diagnostic_mode'):
            # Safeguard against missing fonts on Windows systems with
            # OS-specific font selections
            if sys.platform.startswith("win"):
                # Windows needs the escaped colon for the drive letter
                font_path = r"C\:/Windows/Fonts/arial.ttf"
            elif sys.platform == "darwin":
                # Standard macOS font path
                font_path = "/Library/Fonts/Arial.ttf"
            else:
                # Standard Linux (Ubuntu/Debian) path
                font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

            # Check if the font actually exists before trying to use it
            # (On Linux/Mac, we need to remove the FFmpeg escapes to check with Python)
            check_path = font_path.replace(r"C\:", "C:").replace("\\", "")
            if not os.path.exists(check_path) and not sys.platform.startswith("win"):
                 print("WARNING: Default font not found. Diagnostic text may fail.")

            # Add a diagnostic timestamp overlay in the top-left corner of the
            # video with format HH:MM:SS:FF
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
        table_lines.append(f"\n{'='*81}")
        table_lines.append(f"{'QC SEAM INSPECTION TABLE - Folder: ' + folder_id:^81}")
        table_lines.append(f"{'='*81}")
        table_lines.append(f"{'NEW VIDEO TIME':<18} | {'ACTION':<15} | {'SOURCE FILE':<20} | {'SOURCE TIMESTAMP'}")
        table_lines.append(f"{'-'*18}-|{'-'*17}|{'-'*22}|{'-'*20}")

        # Content
        for i, segment in enumerate(needed_files):
            file_name = os.path.basename(segment['path'])
            start_ts = seconds_to_timestamp(cumulative, fps)
            source_start = seconds_to_timestamp(segment['ss'], fps)
            
            table_lines.append(f"{start_ts:<18} | START SEGMENT   | {file_name:<20} | {source_start}")
            
            cumulative += segment['t']
            end_ts = seconds_to_timestamp(cumulative, fps)
            source_end = seconds_to_timestamp(segment['ss'] + segment['t'], fps)
            
            if i < len(needed_files) - 1:
                table_lines.append(f"{end_ts:<18} | SEAM / STITCH   | {file_name:<20} | {source_end}")
                table_lines.append(f"{' '*18} |      vvv        | {' '*20} |")
            else:
                table_lines.append(f"{end_ts:<18} | VIDEO END       | {file_name:<20} | {source_end}")

        table_lines.append(f"{'='*81}\n")
        full_table_str = "\n".join(table_lines)

        # Write the table to the log file
        with open(config['log_file'], "a") as log:
            log.write(full_table_str)

        # Only print to the console if diagnostic mode is on
        if config.get('diagnostic_mode'):
            print(full_table_str)

        # Build execution command
        #   -c:v: video codec (coder/decoder) to use for encoding. Value
        #       depends on whether GPU acceleration is enabled.
        #   -rc: use Variable Bit Rate mode, allowing encoder to use more data
        #       for complex scenes (moving fish) and less for static ones
        #   -cq: "constant quality" setting for GPU encoder. Lower numbers (10)
        #       prioritize high visual detail. GPU equivalent of CRF.
        #   -crf: "constant rate factor" quality setting for CPU encoder. Lower
        #       values (10) prioritize high visual detail. CPU equivalent of CQ
        #   -b:v: target bitrate for output video. Set to 0 to disable default
        #       bitrate cap and strictly follow `-cq` settings
        #   -maxrate: maximum rate to prvent file size from exploding during
        #       extremely complex frames
        #   -bufsize: buffer size telling teh encoder how much video to look at
        #       when deciding how to distribute the bitrate
        #   -preset: encoding preset; higher quality presets take longer to
        #       encode. Use "p7" for highest quality.
        #   -y: overwrite output file if it exists without asking permission
        #   -map: select the output from the filter
        #   -c:a: set the encoder to use for sound. Use 'aac' for advanced
        #       audio coding format.
        #   -t: duration of the output video (1440 seconds = 24 minutes)
        if config['use_gpu']:
            encoder_args = [
                "-c:v", "h264_nvenc",
                "-rc", "vbr",
                "-cq", str(config['quality_crf']),
                "-b:v", "0",
                "-maxrate", "100M",
                "-bufsize", "100M",
                "-preset", "p7",
            ]
        else:
            encoder_args = [
                "-c:v", "libx264",
                "-crf", str(config['quality_crf']),
                "-preset", "medium",
            ]
            
        cmd = [
            ffmpeg_exe, "-y"
        ] + input_args + [
            "-filter_complex", filter_str,
            "-map", maparg_v,
            "-map", "[outa]",
        ] + encoder_args + [
            "-c:a", "aac",
            "-t", "1440",
            output_path
        ]
        
        # Run the command and log any errors
        print(f"  > Clipping and stitching... This may take some time.\n", flush=True)
        try:
            # Add a timeout to prevent infinite hangs
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            print(f"\nERROR: ffmpeg timed out on {folder_id}. Is the network drive disconnected?")
            continue
            
        if result.returncode != 0:
            with open(config['log_file'], "a") as log:
                log.write(f"ERROR in {folder_id}: {result.stderr}\n")
    
        # Calculate and print elapsed time
        iter_duration = time.perf_counter() - iter_start
        if iter_duration > 60:
            print(f"  > Created {f"{folder_id}{config['video_extension']}"} in {iter_duration/60:.2f} minutes.\n", flush=True)
        else:
            print(f"  > Created {f"{folder_id}{config['video_extension']}"} in {iter_duration:.2f} seconds.\n", flush=True)
    
    # Add space to end of log file for readability between runs
    with open(config['log_file'], "a") as log:
        log.write("\n\n\n")

if __name__ == "__main__":
    # Parse command-line arguments
    args = parse_args()

    # Launch the script
    process_deployments(config_path=args.config_path)
    print("Processing Complete. Check processing_log.txt for details.")