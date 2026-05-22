"""
SEFIS GoPro Clip-and-Stitch Utility
-----------------------------------
A frame-accurate video processing tool designed for compiling survey videos
from the Southeast Fishery Independent Survey (SEFIS). This script automates
the extraction and concatenation of specific video segments from GoPro camera
folders based on "time-on-bottom" timestamps provided in a CSV file. It
ensures seamless stitching of video segments with precise millisecond
alignment across GoPro chapter seams.

Key Features:
    * Parallel Processing: Scales across CPU/GPU workers for bulk processing.
    * Resilient Serial Upload: Pushes new video to a GCP cloud bucket using
        `gcloud storage` after each encode.
    * Diagnostic Overlays: Provides optional burned-in time code with 
        `HH:MM:SS:FF` format for frame-by-frame verification.

Usage:
    python clip-and-stitch.py path/to/name-of-configuration-file.yml

Required Dependencies:
    * pandas: For CSV data management.
    * yaml: For configuration parsing.
    * tqdm: For progress visualization.
    * FFmpeg/ffprobe: Must be installed and accessible via system path 
        or config file.
    * Google Cloud Software Development Kit (SDK): Google Cloud command line
        interface (CLI) for pushing videos to cloud bucket

Author:  matt.grossi@noaa.gov with creation and refactoring assistance from
         Google Gemini Coding Partner
Project: Southeast Fishery Independent Survey (SEFIS)
Version: 2026.0.8
Note:    Gemini Coding Partner was used to assist with developing this code.
         The code has been reviewed, edited, validated, and documented by NOAA
         Fisheries staff.
"""

# =============================================================================
# PACKAGE DEPENDENCIES
# =============================================================================

from datetime import datetime
from typing import Literal
import pandas as pd
import argparse
import shutil
import yaml
import json
import os
import re
import sys
import time
import difflib
import textwrap
import subprocess
from tqdm import tqdm
from concurrent.futures import ProcessPoolExecutor, as_completed

# =============================================================================
# HELPER FUNCTIONS AND METHODS
# =============================================================================

def parse_args():
    """Parses command-line arguments."""
    parser = argparse.ArgumentParser(description="GoPro Clip-and-Stitch Utility")
    parser.add_argument("config_path", type=str, nargs="?",
        default="configurations.yml",
        help="Path to the YAML configuration file (default: configurations.yml)"
    )
    parser.add_argument("--no-processing", action="store_false", dest='process',
        help="Carry out logging without conducting any video processing"
    )
    return parser.parse_args()

# Define a simple mock class to handle the --no-process case
class MockResult:
    returncode = 0
    stderr = "Execution suppressed by --no-process"

def validate_config(config: dict):
    """Checks for typos in the YAML and suggests the closest valid key.
    
    Arguments
    ---------
    config (dict): dictionary of configuration settings to validate
    """
    VALID_KEYS = {
        'clear_log', 'col_foldername', 'col_timebottom', 'csv_path',
        'delete_local_after_upload', 'diagnostic_mode', 'ffmpeg_path',
        'ffprobe_path', 'gcp_bucket_path', 'gcp_upload', 'input_directory',
        'log_file', 'min_gb_required', 'num_workers', 'output_directory',
        'output_fps', 'preread_time_minutes', 'quality_crf', 'reprocess', 
        'time_on_bottom_fps', 'timeout_minutes', 'use_gpu',
        'video_duration_minutes', 'video_extension'
    }
    
    unrecognized = []
    for key in config.keys():
        if key not in VALID_KEYS:
            matches = difflib.get_close_matches(key, list(VALID_KEYS), n=1, cutoff=0.6)
            suggestion = f" (Did you mean '{matches[0]}'?)" if matches else ""
            unrecognized.append(f"  - '{key}'{suggestion}")

    if unrecognized:
        error_msg = (
            "\n[!] CONFIGURATION ERROR: Unrecognized settings found in YAML:\n" +
            "\n".join(unrecognized) +
            "\n\nPlease correct your configuration file and restart the utility."
        )
        raise ValueError(error_msg)

def load_config(config_path: str = 'configurations.yml'):
    """
    Loads and verifies the YAML configuration file.
    
    Arguments
    ---------
    config_path (str): The file path to the YAML configuration file. Defaults
            to 'configurations.yml'.
    Returns
    -------
    dict
    """
    with open(config_path, 'r') as f:
        config = yaml.safe_load(f)
    validate_config(config=config)
    return config

def get_gpu_type():
    """
    Queries nvidia-smi to determine if the installed GPU is a 'Professional' 
    model with unlimited/high session limits.

    Returns
    -------
    str, 'PRO' for professional grade mode, 'CONSUMER' for consumer grade, or
    'UNKNOWN' if unknown or no GPU is found
    """
    try:
        # Query the GPU name directly from NVIDIA driver
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=name", "--format=csv,noheader"],
            capture_output=True, text=True, check=True
        )
        gpu_name = result.stdout.lower()
        
        # Identifiers for Professional/Qualified hardware
        pro_identifiers = ['rtx 6000', 'rtx 5000', 'quadro', 'tesla', 'a-series', 'ada generation']
        
        if any(ident in gpu_name for ident in pro_identifiers):
            return "PRO"
        return "CONSUMER"
    except (subprocess.CalledProcessError, FileNotFoundError):
        # Default to consumer if nvidia-smi fails or isn't found
        return "UNKNOWN"

def get_video_metadata(file_path: str, ffprobe_path: str):
    """Uses ffprobe to get internal metadata of a video file.
    
    Arguments
    ---------
    file_path (str): file path to the video from which to extract metadata
    ffprobe_path (str): file path to the `ffprobe` executable

    Returns
    -------
    list: [duration, fps, bit_rate, width, height]
        duration: float, duration of the video in seconds
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
    duration = float(data['format']['duration'])
    bit_rate = int(data['format']['bit_rate'])
    
    # Extract width and height from video stream
    width = 0
    height = 0
    for stream in data.get('streams', []):
        if stream.get('codec_type') == 'video':
            width = int(stream.get('width'))
            height = int(stream.get('height'))
            fps_str = stream.get('avg_frame_rate')
            if '/' in fps_str:
                num, den = map(int, fps_str.split('/'))
                fps = num / den
            else:
                fps = float(fps_str)
            break
    
    return duration, fps, bit_rate, width, height

def calculate_file_size(duration: float | int, bit_rate: float | int) -> int:
    """Calculates the file size in bytes based on duration and bit rate.
    
    Arguments
    ---------
    duration (float or int): video duration in seconds
    bit_rate (float or int): video bit rate

    Returns
    -------
    (int) expected file size
    """
    return int((duration * bit_rate) / 8)

def log_and_print(message: str, log_path: str, indent_spaces: int = 4):
    """Indents and writes a message to both console and log.
    
    Arguments
    ---------
    message (str): message to print and write to log file
    log_path (str): file path to log file to populate
    indent (int): number spaces to indent message. Defaults to 4.
    """
    indent = " " * indent_spaces
    clean_msg = textwrap.indent(textwrap.dedent(message).strip(), indent)
    print(clean_msg + "\n", flush=True)
    with open(log_path, "a") as log:
        log.write(clean_msg + "\n\n")

def check_gcp_auth(bucket_path: str):
    """Verifies Google Cloud storage access before starting.
    
    Arguments
    ---------
    bucket_path (str): full path to GCP storage bucket
    """
    print(f"  > Verifying GCP access to {bucket_path}...", flush=True)
    
    # Use shutil.which to find the actual path of gcloud (handles .cmd on Windows)
    gcloud_exec = shutil.which("gcloud")
    
    if not gcloud_exec:
        print("\nERROR: 'gcloud' command not found. Is Google Cloud SDK installed and in your PATH?")
        return False

    try:
        # Check access using the resolved gcloud path
        result = subprocess.run([gcloud_exec, "storage", "ls", bucket_path], capture_output=True, text=True)
        
        if result.returncode != 0:
            print("\nERROR: GCP Authentication failed or bucket inaccessible.")
            print(f"Details: {result.stderr.strip()}")
            return False
        return True
        
    except Exception as e:
        # This catches unexpected system errors, not standard gcloud failures
        print(f"\nERROR: An unexpected error occurred while checking GCP: {e}")
        return False

def get_ffmpeg_command(config: dict, tool: Literal["ffmpeg", "ffprobe"] = "ffmpeg"):
    """
    Finds ffmpeg or ffprobe. Checks local folder, then config, then system.
    Compatible with Windows (.exe) and Mac/Linux (no extension).

    Arguments
    ---------
    config (dict): dictionary containing configuration parameters
    tool ({"ffmpeg", "ffprobe"}): The executable to find. Defaults to "ffmpeg". 
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
    """Round time stamp up to the nearest 30 seconds
    
    Arguments
    ---------
    time_str (str): timestamp string formatted "HH:MM:SS:FF" to be rounded

    Returns:
    (str) Rounded timestamp formatted "HH:MM:SS:FF"
    """
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

def timestamp_to_seconds(timestamp_str: str, fps: float | int) -> float:
    """Converts HH:MM:SS:FF (frames) to total seconds (float).
    
    Arguments
    ---------
    timestamp_str (str): timestamp formatted "HH:MM:SS:FF" to be converted
    fps (float | int): frames per second frame rate

    Returns
    (float) Total number of fractional seconds
    """
    parts = timestamp_str.split(':')
    h, m, s, f = map(int, parts)
    return h * 3600 + m * 60 + s + (f / fps)

def seconds_to_timestamp(seconds: float | int, fps: float | int) -> str:
    """
    Converts seconds to HH:MM:SS:FF using 0-based indexing and small epsilon
    flooring to prevent rounding-induced frame jumps.
    
    Arguments
    ---------
    seconds (float | int): total number of seconds
    fps (float | int): frames per second frame rate

    Returns
    -------
    (str) Timestamp formatted "HH:MM:SS:FF"
    """
    # Add a tiny epsilon to handle float precision issues (e.g., 14.999999 -> 15.0)
    total_frames = int(seconds * fps + 1e-6)
    
    # Calculate components from total frames
    f = total_frames % int(round(fps))
    total_seconds = total_frames // int(round(fps))
    
    s = total_seconds % 60
    total_minutes = total_seconds // 60
    
    m = total_minutes % 60
    h = total_minutes // 60
    
    return f"{h:02}:{m:02}:{s:02}:{f:02}"

def get_gopro_sort_key(filename: str):
    """
    Parses GoPro filenames for correct sorting.

    Arguments
    ---------
    filename (str): Name of GoPro video file. Example: "GX010192.MP4"

    Returns
    -------
    (tuple) (Recording ID, Chapter Number) Example: GX020192 -> (0192, 02)
    """
    match = re.search(r'([A-Z]{2})(\d{2})(\d{4})', filename.upper())
    if match:
        _, chapter, rec_id = match.groups()
        return (int(rec_id), int(chapter))
    return (0, 0)


# =============================================================================
# WORKER TASK: PROCESS SINGLE DEPLOYMENT
# =============================================================================

def process_single_deployment(row: dict, config: dict, ffmpeg_exe: str, ffprobe_exe: str, process: bool, pbar_pos: int) -> dict:
    """Standalone task for processing one deployment.
    
    Arguments
    ---------
    row (dict): dictionary containing video information and metadata
    config (dict): configuration dictionary
    ffmpeg_exe (str): file path to `ffmpeg_exe` executable
    ffprobe_exe (str): file path to `ffprobe_exe`
    process (bool): if False, suppresses FFmpeg execution while still logging
            for dry run troubleshooting
    pbar_pos (int): vertical position for the tqdm progress bar

    Returns
    -------
    (dict) Dictionary containing processed folder name and processing status.
            Example: {"status": "SUCCESS", "folder_id": "T60253001_A", ...}
    """
    # Start timer for this video for diagnostic mode
    if config['diagnostic_mode']:
        iter_start = time.perf_counter()
    folder_id = str(row[config['col_foldername']]).strip()
    time_bottom_ceil = str(row['timebottom_ceil']).strip()

    # Input and output directories
    folder_path = os.path.join(config['input_directory'], folder_id)
    if not os.path.exists(folder_path):
        with open(config['log_file'], "a") as log:
            log.write(f"SKIP: Folder {folder_path} not found.\n")
        return {"status": "SKIP", "folder_id": folder_id}
    output_path = os.path.join(
        config['output_directory'], f"{folder_id}{config['video_extension']}"
        )

    # Skip if output exists and reprocess is false
    if not config['reprocess'] and os.path.exists(output_path):
        with open(config['log_file'], "a") as log:
            log.write(f"{os.path.basename(output_path)} exists and reprocess is set to False. Nothing to process. Skipping.\n")
        return {"status": "SKIP", "folder_id": folder_id}

    # Skip and log any folder listed in the CSV that doesn't exist in the input
    # directory
    if not os.path.exists(folder_path):
        with open(config['log_file'], "a") as log:
            log.write(f"SKIP: Folder {folder_id} not found.\n")
        return {"status": "SKIP", "folder_id": folder_id}

    # 3. GATHER & SORT FILES
    video_files = [f for f in os.listdir(folder_path) 
                    if f.upper().endswith(config['video_extension'].upper())]
    if not video_files:
        with open(config['log_file'], "a") as log:
            log.write(f"SKIP: No videos in {folder_id}.\n")
        return {"status": "SKIP", "folder_id": folder_id}
    video_files.sort(key=get_gopro_sort_key)

    # 4. CALCULATE TIMELINE
    # Resolve frame rates (`source_fps` will come from video metadata)
    first_file_path = os.path.join(folder_path, video_files[0])
    _, source_fps, _, _, _ = get_video_metadata(file_path=first_file_path, ffprobe_path=ffprobe_exe)
    time_on_bottom_fps = float(config['time_on_bottom_fps'])
    raw_target = str(config['output_fps']).lower()
    output_fps = source_fps if raw_target == 'auto' else float(raw_target)

    # Convert between Power Director (PD) seconds and GoPro seconds
    time_scaling = time_on_bottom_fps / source_fps
    
    # Video slice times: seek using PD frame rate-derived time, then convert to
    # actual
    pd_start_seconds = timestamp_to_seconds(timestamp_str=time_bottom_ceil, fps=time_on_bottom_fps)
    pd_start_seconds += (int(config['preread_time_minutes']) * 60)
    pd_duration_seconds = int(config['video_duration_minutes']) * 60

    # Prevent ffmpeg from skipping first frame due to floating-point rounding:
    #   -> 0.2 frame pullback to ensure start frame inclusion
    #   -> 0.1s trailing padding buffer to prevent truncation of final frames
    nudge = 0.2 / time_on_bottom_fps
    padding = 0.1
    start_seconds = (pd_start_seconds - nudge) * time_scaling
    video_duration_sec = (pd_duration_seconds * time_scaling)
    end_seconds = start_seconds + video_duration_sec + nudge + padding

    # Extract video metadata
    if config['diagnostic_mode']:
        print(f"\n  > Probing metadata for {len(video_files)} video chapters in {folder_id}...", flush=True)
    file_data = []
    for f in video_files:
        full_p = os.path.join(folder_path, f)
        dur, source_fps, br, w, h = get_video_metadata(file_path=full_p, ffprobe_path=ffprobe_exe)
        file_data.append({
            'path': full_p,
            'duration': dur,
            'fps': source_fps,
            'bit_rate': br,
            'width': w,
            'height': h
        })

    # Check the start times and durations of each video to determine which
    # files are needed to stitch together and where to clip partial videos
    if config['diagnostic_mode']:
        print("  > Determining needed files and trim points...", flush=True)
    cumulative_time = 0
    needed_files = []
    for data in file_data:
        file_start = cumulative_time
        file_end = cumulative_time + data['duration']
        
        # Check whether this file overlaps with the desired clip range
        if file_end > start_seconds and file_start < end_seconds:
            # Store the relative start/end for this specific file
            rel_start = max(0, start_seconds - file_start)
            rel_end = min(data['duration'], end_seconds - file_start)

            # Calculate bits per pixel (BPP) for the current chapter
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
        return {"status": "SKIP", "folder_id": folder_id}

    # 5. CLIP AND STITCH
    # See https://ffmpeg.org/ffmpeg.html
    cumulative_size = 0
    cumulative_bpp = 0
    input_args = []
    filter_complex_parts = []
    filter_inputs = ""
    for i, f_info in enumerate(needed_files):
        cumulative_size += f_info['size']
        cumulative_bpp += f_info['bpp']
        input_args.extend(["-i", f_info['path']])

        # Trim the video using start time and duration (seconds) and reset the
        # clock of the trimmed segment (`setpts`) to 0 so the stitcher sees a
        # clean sequence starting from 0.0s
        trim_label = f"[v{i}]"
        filter_complex_parts.append(
            f"[{i}:v]trim=start={f_info['ss']}:duration={f_info['t']},"
            f"setpts=PTS-STARTPTS{trim_label}"
        )
        filter_inputs += trim_label

    # Target bitrate based on original GoPro metadata to ensure visual
    # fidelity
    target_bitrate = f"{int(cumulative_size * 8 / (pd_duration_seconds * time_scaling))}"
    is_auto_mode = str(config['quality_crf']).lower() == 'auto'
    if is_auto_mode and config['diagnostic_mode']:
        print(f"  > Targeting bitrate {int(target_bitrate)/1_000_000:.2f} Mbps to match source density.", flush=True)

    # Build the filter string: e.g., `[0:v][1:v]concat=n=2:v=1[outv]`:
    # Concatenate video (v) from files 0-n into 1 video stream.
    # Force the frame rate (fps=fps={fps}) right after the concat to prevent
    # drift during the stitch. 'round=near' prevents frame drops due to math
    # drift.
    fps_logic = f"fps=fps={output_fps}:round=near"
    if raw_target != 'auto':
        fps_logic += f",setpts=N/({output_fps}*TB)"

    concat_part = (
        f"{filter_inputs}concat=n={len(needed_files)}:v=1,"
        f"{fps_logic}[outv]"
    )
    filter_complex_parts.append(concat_part)
    
    # Get fonts to prevent potential crashes on Windows
    if config['diagnostic_mode']:
        # Safeguard against missing fonts on Windows systems with OS-specific
        # font selections
        if sys.platform.startswith("win"):
            font_path = r"C\:/Windows/Fonts/arial.ttf"
        elif sys.platform == "darwin":
            font_path = "/Library/Fonts/Arial.ttf"
        else:
            font_path = "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"

        # Check if the font actually exists before trying to use it
        check_path = font_path.replace(r"C\:", "C:").replace("\\", "")
        if not os.path.exists(check_path) and not sys.platform.startswith("win"):
             print("WARNING: Default font not found. Diagnostic text may fail.")

        # Embed a diagnostic timestamp for the stitched video
        # This needs to be "fudged" if the desired output frame rate differs
        # from the frame rate used to determine the time-on-bottom (i.e., if
        # time_on_bottom_fps != output_fps) in order for this time to match the
        # media player frame rate
        fudged_time = f"(t*{output_fps}/{time_on_bottom_fps})"
        drawtext_filter = (
            f"[outv]drawtext=fontfile='{font_path}':"
            r"text='%{eif\:" + fudged_time + r"/3600\:d\:2}\:" + \
            r"%{eif\:mod(" + fudged_time + r"/60,60)\:d\:2}\:" + \
            r"%{eif\:mod(" + fudged_time + r",60)\:d\:2}\:" + \
            r"%{eif\:" + str(time_on_bottom_fps) + r"*mod(" + fudged_time + r",1)\:d\:2}':"
            "x=10:y=10:fontsize=48:fontcolor=white:box=1:boxcolor=black@0.5[diagout]"
        )
        filter_complex_parts.append(drawtext_filter)
        maparg = "[diagout]"
    else:
        maparg = "[outv]"

    # Join all filter parts with semicolons
    filter_str = "; ".join(filter_complex_parts)

    # 6. GENERATE QC TABLE
    cumulative_output_frames = 0
    target_total_frames = int(pd_duration_seconds * time_on_bottom_fps)
    table_lines = []
    table_lines.append(f"\n{'='*80}")
    table_lines.append(f"{'QC SEAM INSPECTION TABLE - Folder: ' + folder_id + ' (' + str(config['video_duration_minutes']) + ' min)':^80}")
    table_lines.append(f"{'='*80}")
    table_lines.append(f"{'NEW VIDEO TIME':<18} | {'ACTION':<17} | {'SOURCE FILE':<17} | {'SOURCE TIMESTAMP'}")
    table_lines.append(f"{'-'*19}|{'-'*19}|{'-'*19}|{'-'*20}")

    for i, segment in enumerate(needed_files):
        start_ts = seconds_to_timestamp(cumulative_output_frames / time_on_bottom_fps, time_on_bottom_fps)
        report_ss = segment['ss'] + (nudge * time_scaling if i == 0 else 0)
        source_start = seconds_to_timestamp(report_ss / time_scaling, time_on_bottom_fps)
        table_lines.append(f"{start_ts:<18} | START SEGMENT     | {os.path.basename(segment['path']):<17} | {source_start}")
        
        # Calculate discrete frames for THIS segment by removing the nudge from the count
        actual_t = segment['t'] - (nudge * time_scaling if i == 0 else 0)
        segment_frames = int(round(actual_t * segment['fps']))
        
        # Hard-cap to prevent padding 'leakage' into the table report
        if (cumulative_output_frames + segment_frames) > target_total_frames:
            segment_frames = target_total_frames - cumulative_output_frames
        
        # Last frame index
        last_frame_idx = cumulative_output_frames + segment_frames - 1
        end_ts = seconds_to_timestamp(last_frame_idx / time_on_bottom_fps, time_on_bottom_fps)
        
        # Source end
        last_frame_rel = (segment_frames - 1) / segment['fps']
        source_end = seconds_to_timestamp((report_ss + last_frame_rel) / time_scaling, time_on_bottom_fps)
        
        if i < len(needed_files) - 1:
            table_lines.append(f"{end_ts:<18} | LAST FRAME        | {os.path.basename(segment['path']):<17} | {source_end}")
            table_lines.append(f"{' '*18} |      -- SEAM --   | {' '*17} |")
        else:
            final_end_ts = seconds_to_timestamp(target_total_frames / time_on_bottom_fps, time_on_bottom_fps)
            table_lines.append(f"{final_end_ts:<18} | VIDEO END         | {os.path.basename(segment['path']):<17} | {source_end}")
        
        cumulative_output_frames += segment_frames

    table_lines.append(f"{'-'*80}")
    table_lines.append(f"TOTAL OUTPUT FRAMES: {cumulative_output_frames} / {target_total_frames}")
    table_lines.append(f"{'='*80}\n")
    full_table_str = "\n".join(table_lines)

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
    #   -maxrate: maximum rate to prevent file size from exploding during
    #       extremely complex frames
    #   -bufsize: buffer size telling the encoder how much video to look at
    #       when deciding how to distribute the bitrate
    #   -preset: encoding preset; higher quality presets take longer to
    #       encode. Use "p7" for highest quality.
    #   -y: overwrite output file if it exists without asking permission
    #   -map: select the output from the filter
    #   -an: exclude audio from new file
    #   -t: duration of the output video
    if config['use_gpu']:
        encoder_args = [
            "-c:v", "h264_nvenc",
            "-rc", "vbr",
        ]
        if is_auto_mode:
            # Archival bitrate targeting (ABR/VBR)
            encoder_args += ["-b:v", target_bitrate, "-maxrate", "100M", "-bufsize", "100M"]
        else:
            # Manual Constant Quality Parameter (CQP) override
            encoder_args += ["-b:v", "0", "-cq", str(config['quality_crf'])]
        encoder_args += ["-preset", "p7"]
    else:
        encoder_args = [
            "-c:v", "libx264",
        ]
        if is_auto_mode:
            # Archival Bitrate Targeting (ABR)
            encoder_args += ["-b:v", target_bitrate]
        else:
            # Manual Constant Rate Factor (CRF) override
            encoder_args += ["-crf", str(config['quality_crf'])]
        encoder_args += ["-preset", "medium"]
    
    # Video duration for metadata
    final_metadata_t = pd_duration_seconds if raw_target != 'auto' else (pd_duration_seconds * time_scaling)

    cmd = [
        ffmpeg_exe, "-y"
    ] + input_args + [
        "-filter_complex", filter_str,
        "-map", maparg,
        "-an"
    ] + encoder_args + [
        "-r", str(output_fps),
        "-fps_mode", "cfr",
        "-video_track_timescale", "30000",
        "-t", str(final_metadata_t),
        output_path
    ]

    # Run the command and log any errors
    result = MockResult()
    if process:
        try:
            # Add a timeout to prevent infinite hangs
            timeout_val = int(config['timeout_minutes']) * 60 if config.get('timeout_minutes') else None
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_val)
        except subprocess.TimeoutExpired:
            print(f"\nERROR: ffmpeg timed out on {folder_id}. Is the network drive disconnected?")
            return {"status": "ERROR", "folder_id": folder_id, "error_msg": "FFmpeg timed out."}
            
        if result.returncode != 0:
            with open(config['log_file'], "a") as log:
                log.write(f"ERROR in {folder_id}: {result.stderr}\n")
            return {"status": "ERROR", "folder_id": folder_id, "error_msg": result.stderr}
    
    # Calculate actual metrics for the final video
    if os.path.exists(output_path):
        actual_size = os.path.getsize(output_path)
        actual_bitrate = (actual_size * 8) / final_metadata_t
        actual_bpp = actual_bitrate / (needed_files[0]['width'] * needed_files[0]['height'] * output_fps)
    else:
        actual_size = 0
        actual_bpp = 0

    # Calculate final QC metrics
    avg_bpp_src = cumulative_bpp / len(needed_files)
    expectations_table_lines = []
    expectations_table_lines.append(f"{' '*23} | EXPECTED {' '*7} ACTUAL")
    expectations_table_lines.append(f"    {'-'*20}|{'-'*30}")
    expectations_table_lines.append(f"    OUTPUT FILE SIZE    | {cumulative_size / (2**30):.2f} GB {' ':<4} --> {actual_size / (2**30):.2f} GB")
    expectations_table_lines.append(f"    BITRATE             | {int(target_bitrate)/1_000_000:.2f} Mbps {' ':<1} --> {actual_bitrate/1_000_000:.2f} Mbps")
    expectations_table_lines.append(f"    INFORMATION DENSITY | {avg_bpp_src:.4f} BPP {' ':<1} --> {actual_bpp:.4f} BPP")
    expectations_table_str = "\n".join(expectations_table_lines) +"\n"
    if config['diagnostic_mode']:
        iter_duration = time.perf_counter() - iter_start
        if iter_duration > 60:
            print(f"  > Created {folder_id}{config['video_extension']} in {iter_duration/60:.2f} minutes.\n", flush=True)
        else:
            print(f"  > Created {folder_id}{config['video_extension']} in {iter_duration:.2f} seconds.\n", flush=True)
        print("    Output file metrics versus expectations:\n", flush=True)
        print(expectations_table_str, flush=True)

    # Check expectations
    is_bpp_ideal_80 = avg_bpp_src * 0.80 <= actual_bpp <= avg_bpp_src * 1.20
    is_size_ideal_80 = cumulative_size * 0.80 <= actual_size <= cumulative_size * 1.20
    is_bpp_ideal_90 = avg_bpp_src * 0.90 <= actual_bpp <= avg_bpp_src * 1.10
    is_size_ideal_90 = cumulative_size * 0.90 <= actual_size <= cumulative_size * 1.10

    with open(config['log_file'], "a") as log:
        log.write(f"\nSUMMARY OF FOLDER {folder_id}:\n")
        log.write(f"    Estimated output video size without visual quality loss: {cumulative_size / (2**30):.2f} GB\n")
        log.write(f"    Estimated target bitrate to maintain visual fidelity: {int(target_bitrate)/1_000_000:.2f} Mbps\n")
        log.write(f"    Average information density of original videos: {avg_bpp_src:.4f} bits per pixel (BPP)\n")
        log.write(' '*30 + '* '*10 + '\n\n')
        log.write(f"    Output video file size: {actual_size / (2**30):.2f} GB\n")
        log.write(f"    Output video bitrate:   {actual_bitrate/1_000_000:.2f} Mbps\n")
        log.write(f"    Information density:    {actual_bpp:.4f} BPP\n\n")
        log.write(f"    -> Within 80% of original average BPP:  {'YES' if is_bpp_ideal_80 else 'NO  X'}\n")
        log.write(f"    -> Within 80% of estimated file size:   {'YES' if is_size_ideal_80 else 'NO  X'}\n")
        log.write(f"    -> Within 90% of original average BPP:  {'YES' if is_bpp_ideal_90 else 'NO  X'}\n")
        log.write(f"    -> Within 90% of estimated file size:   {'YES' if is_size_ideal_90 else 'NO  X'}\n")

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

    # GCP serial upload
    upload_status = "PENDING"
    if config.get('gcp_upload') and process:
        try:
            from google.cloud import storage
            client = storage.Client()
            bucket_path = config['gcp_bucket_path'].replace("gs://", "").strip("/")
            bucket_name = bucket_path.split("/")[0]
            prefix = "/".join(bucket_path.split("/")[1:])
            blob_name = f"{prefix}/{os.path.basename(output_path)}" if prefix else os.path.basename(output_path)
            
            bucket = client.bucket(bucket_name)
            blob = bucket.blob(blob_name)
            file_size = os.path.getsize(output_path)

            # Positioned status bar
            show_bar = config['num_workers'] <= 20
            with tqdm(total=file_size, unit='B', unit_scale=True, 
                      desc=f"  -> Uploading {folder_id}", position=pbar_pos, leave=False, disable=not show_bar) as up_pbar:
                blob.upload_from_filename(output_path, callback=lambda b: up_pbar.update(b))
            
            upload_status = "SUCCESS"
            if config.get('delete_local_after_upload'):
                os.remove(output_path)
        except Exception as e:
            upload_status = f"FAILED: {str(e)}"

    # Print table if successful
    if result.returncode == 0:
        with open(config['log_file'], "a") as log:
            log.write(full_table_str)
        if config['diagnostic_mode']:
            print(full_table_str)

    return {"status": "SUCCESS", "folder_id": folder_id, "upload_status": upload_status, "output_path": output_path}

# =============================================================================
# MAIN ROUTINE
# =============================================================================

def process_deployments(config_path: str = 'configurations.yml', process=True):
    """Orchestrates parallel processing and returns True if no critical errors occurred."""
    
    # SETTINGS THAT CAN ALSO BE SET IN THE YAML CONFIG FILE
    config = load_config(config_path.strip('"'))
    config['clear_log'] = config.get('clear_log', False)
    config['delete_local_after_upload'] = config.get('delete_local_after_upload', False)
    config['diagnostic_mode'] = config.get('diagnostic_mode', False)
    config['gcp_upload'] = config.get('gcp_upload', False)
    config['log_file'] = config.get('log_file', 'processing_log.txt')
    config['min_gb_required'] = config.get('min_gb_required', 10)
    config['num_workers'] = config.get('num_workers', 1)
    config['output_fps'] = config.get('output_fps', 'auto')
    config['preread_time_minutes'] = config.get('preread_time_minutes', 8)
    config['quality_crf'] = config.get('quality_crf', 'auto')
    config['reprocess'] = config.get('reprocess', False)
    config['time_on_bottom_fps'] = config.get('time_on_bottom_fps', 30)
    config['timeout_minutes'] = config.get('timeout_minutes', 60)
    config['use_gpu'] = config.get('use_gpu', False)
    config['video_duration_minutes'] = config.get('video_duration_minutes', 24)
    config['video_extension'] = config.get('video_extension', '.MP4')

    ffmpeg_exe = get_ffmpeg_command(config=config, tool="ffmpeg")
    ffprobe_exe = get_ffmpeg_command(config=config, tool="ffprobe")

    # CONSTRAIN PROCESSING TO HARDWARE CAPABILITIES
    max_cpu = os.cpu_count() or 1
    gpu_status = get_gpu_type() if config['use_gpu'] else "N/A"

    if config['use_gpu']:
        if gpu_status == "PRO":
            max_allowed = max_cpu 
        else:
            max_allowed = 12
        if config['num_workers'] > max_allowed:
            print(f"NOTICE: num_workers ({config['num_workers']}) exceeds hardware safety limit. Capping at {max_allowed}.", flush=True)
            config['num_workers'] = max_allowed
    else:
        max_allowed = max(1, max_cpu - 1)
        if config['num_workers'] > max_allowed:
            print(f"  > NOTICE: num_workers ({config['num_workers']}) exceeds hardware limit ({max_cpu}). Capping at {max_allowed}.", flush=True)

    # 1. SETUP & VALIDATION
    # Verify there is enough disk space to continue without locking up system
    os.makedirs(config['output_directory'], exist_ok=True)
    _, _, free = shutil.disk_usage(config['output_directory'])
    if free // (2**30) < config['min_gb_required']:
        log_and_print(f"ERROR: Insufficient disk space ({free // (2**30)}GB remaining). Stopping.", config['log_file'])
        return False
    
    # Confirm GCP bucket authentication
    if config['gcp_upload']:
        gcloud_exec = shutil.which("gcloud")
        if 'gcp_bucket_path' not in config.keys():
            print("\nERROR: `gcp_upload` set to True but no GCP bucket was defined.")
            return False
        if not check_gcp_auth(bucket_path=config['gcp_bucket_path']):
            return False

    # Start the log file
    mode = "w" if config['clear_log'] else "a"
    with open(config['log_file'], mode) as log:
        log.write(f"{'#'*80}\nSESSION START: {datetime.now()}\n")
        log.write(f"CONFIGURATION: {config_path}\n\n")
        for key, value in config.items():
            log.write(f"  -> {key}: {value}\n")
        log.write("\n")
        log.write(f"{'#'*80}\n\n")

    # Load and format CSV
    try:
        df = pd.read_csv(config['csv_path'], encoding='utf-8')
    except UnicodeDecodeError:
        df = pd.read_csv(config['csv_path'], encoding='ISO-8859-1')
    df.columns = df.columns.str.strip()
    df[config['col_foldername']] = df[config['col_foldername']].str.strip()
    df['timebottom_ceil'] = df[config['col_timebottom']].apply(time_ceiling)    

    print(f"  > Processing {int(df.shape[0])} deployments. This may take some time.\n", flush=True)
    tasks = df.to_dict('records')
    failed_uploads, overall_success = [], True

    # Total progress bar
    custom_format = "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt}"
    pbar = tqdm(total=len(tasks), position=0, desc="Total Progress", bar_format=custom_format)

    with ProcessPoolExecutor(max_workers=config['num_workers']) as executor:
        futures = {}
        for task_idx, row in enumerate(tasks, start=1):
            task_pbar_pos = ((task_idx - 1) % config['num_workers']) + 1
            future = executor.submit(
                process_single_deployment, 
                row, config, ffmpeg_exe, ffprobe_exe, 
                process, task_pbar_pos
            )
            futures[future] = row

        for future in as_completed(futures):
            result = future.result()
            pbar.update(1)
            
            # Loud-Crash Logic for systemic errors
            if result['status'] == "ERROR":
                error_msg = result.get('error_msg', 'Unknown execution error.')
                pbar.close()
                
                # Report the error loudly to the console
                print("\n" + "!" * 80)
                print(f"CRITICAL FFMPEG FAILURE IN DEPLOYMENT: {result['folder_id']}")
                print("-" * 80)
                print(f"RAW ERROR LOG:\n\n{error_msg}")
                print("!" * 80 + "\n")
                
                # Shutdown the executor and exit with a non-zero code
                print("[!] SYSTEMIC ERROR: Aborting script to prevent log flooding. Fix the error above.")
                executor.shutdown(wait=False, cancel_futures=True)
                sys.exit(1)
                
            if result.get('status') == "SUCCESS" and result.get('upload_status').startswith("FAILED"):
                failed_uploads.append(result['output_path'])

    # Retry any failed GCP uploads
    if failed_uploads:
        gcloud_exec = shutil.which("gcloud")
        for path in failed_uploads:
            retry = subprocess.run([gcloud_exec, "storage", "cp", path, config['gcp_bucket_path']])
            if retry.returncode != 0:
                overall_success = False

    return (overall_success, os.path.basename(config['log_file']))

if __name__ == "__main__":
    # Parse command-line arguments
    args = parse_args()

    # Script timer
    script_start = time.perf_counter()

    # Launch the script and only print success message if it returns True
    success, log = process_deployments(
        config_path=args.config_path, process=args.process
        )
    if success:
        script_duration = time.perf_counter() - script_start
        if script_duration > 60:
            runtime = f"{script_duration/60:.2f} minutes"
        else:
            runtime = f"{script_duration:.2f} seconds"
        print(f"\nProcessing complete. Total runtime: {runtime}. Check "
              f"{log} for details.", flush=True)
