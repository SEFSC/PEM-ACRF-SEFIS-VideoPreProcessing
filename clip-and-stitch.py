"""
SEFIS GoPro Clip-and-Stitch Utility
-----------------------------------
A frame-accurate video processing tool designed for compiling survey videos
from the Southeast Fishery Independent Survey (SEFIS). This script automates
the extraction and concatenation of specific video segments from GoPro camera
folders based on "start_time" timestamps provided in a CSV file. It ensures
seamless stitching of video segments with precise millisecond alignment across
GoPro chapter seams.

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
Version: 2026.1.0
Note:    Gemini Coding Partner was used to assist with developing this code.
         The code has been reviewed, edited, validated, and documented by NOAA
         Fisheries staff.
"""

# =============================================================================
# PACKAGE DEPENDENCIES
# =============================================================================

from collections import defaultdict
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

def clean_and_validate_config(config: dict):
    """Checks for missing mandatory keys and typos in the YAML. Suggests the
    closest-match valid key for any invalid key found. Cleans any string values
    when Bools are expected, ensures video file extension, if passed, contains
    a leading ".", and ensures the GCP bucket path, if passed, ends with a "/"
    to ensure it is treated as a file prefix.
    
    Arguments
    ---------
    config (dict): dictionary of configuration settings to validate
    """
    # Check for missing mandatory entries
    REQUIRED_KEYS = {'col_folder_name', 'col_start_time', 'csv_path',
                     'input_directory', 'output_directory'}
    missing = [f"  - '{k}'" for k in REQUIRED_KEYS if k not in config]
    
    if missing:
        error_msg = (
            "\n[!] CONFIGURATION ERROR: Missing mandatory settings in YAML:\n" +
            "\n".join(missing) +
            "\n\nThe pipeline cannot start without these core paths defined."
        )
        raise ValueError(error_msg)

    # Check for typos or unrecognized keys
    VALID_KEYS = {
        'clear_log', 'col_folder_name', 'col_start_time', 'csv_path',
        'delete_local_after_upload', 'diagnostic_mode',
        'ffmpeg_path', 'ffprobe_path',
        'gcp_bucket_path', 'gcp_upload',
        'input_directory',
        'log_file',
        'max_retries', 'min_gb_required',
        'num_workers',
        'output_directory', 'output_fps',
        'quality_crf',
        'reprocess',
        'skip_partial_videos', 'start_time_fps',
        'time_buffer_minutes', 'timeout_minutes',
        'use_gpu',
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

    # Clean up Bool , if needed
    BOOLEAN_KEYS = {
        'clear_log', 'delete_local_after_upload', 'diagnostic_mode', 
        'gcp_upload', 'reprocess', 'skip_partial_videos', 'use_gpu'
    }
    for key in BOOLEAN_KEYS:
        if key in config and isinstance(config[key], str):
            clean_val = config[key].strip().lower()
            if clean_val in ('true', 'yes', 'on', '1'):
                config[key] = True
            elif clean_val in ('false', 'no', 'off', '0'):
                config[key] = False
    
    # Ensure video extension always starts with a leading dot
    if 'video_extension' in config and isinstance(config['video_extension'], str):
        if not config['video_extension'].startswith('.'):
            config['video_extension'] = '.' + config['video_extension']
    
    # Ensure GCP bucket ends with a "/" to be treated as a prefix
    if config.get('gcp_upload', False) and 'gcp_bucket_path' in config:
        config['gcp_bucket_path'] = config['gcp_bucket_path'].rstrip('/') + '/'

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
    clean_and_validate_config(config=config)
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
    # Use shutil.which to find the actual path of gcloud (handles .cmd on Windows)
    gcloud_exec = shutil.which("gcloud")
    
    if not gcloud_exec:
        print("\nERROR: 'gcloud' command not found. Is Google Cloud SDK installed and in your PATH?")
        return False

    # Isolate the root bucket path (e.g., 'gs://bucket-name/') to separate auth from folder existence
    match = re.match(r'^(gs://[^/]+)', bucket_path)
    root_bucket = match.group(1) + '/' if match else bucket_path

    try:
        # Verify system authentication and base bucket accessibility
        root_result = subprocess.run([gcloud_exec, "storage", "ls", root_bucket], capture_output=True, text=True)
        
        if root_result.returncode != 0:
            print("\n[!] GCP AUTHENTICATION ERROR: Failed to connect to the cloud storage system.")
            print("Please verify your gcloud authentication credentials, login status, or bucket permissions.")
            print(f"Details: {root_result.stderr.strip()}")
            return False
            
        # If credentials are valid, verify if the explicit subfolder prefix exists
        path_result = subprocess.run([gcloud_exec, "storage", "ls", bucket_path], capture_output=True, text=True)
        
        if path_result.returncode != 0:
            print("\n[!] GCP CONFIGURATION ERROR: Target folder path does not exist.")
            print(f"  -> Specified destination: {bucket_path}")
            print("\nTo prevent configuration typos from cluttering the cloud bucket layout,")
            print("this utility will not automatically initialize new directory prefixes.")
            print("Please double-check your spelling in the YAML config file, or manually create")
            print("the destination folder via the GCP Console or CLI before running this pipeline.")
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
    config_val = config[config_key]
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

def process_single_deployment(row: dict, config: dict, ffmpeg_exe: str, ffprobe_exe: str, process: bool, remote_inventory: set = None) -> dict:
    """Standalone task for processing one deployment.
    
    Arguments
    ---------
    row (dict): dictionary containing video information and metadata
    config (dict): configuration dictionary
    ffmpeg_exe (str): file path to `ffmpeg_exe` executable
    ffprobe_exe (str): file path to `ffprobe_exe`
    process (bool): if False, suppresses FFmpeg execution while still logging
            for dry run troubleshooting
    remote_inventory (set): standalone memory cache lookup array of files
            currently existing on the destination GCP bucket. Defaults to None.

    Returns
    -------
    (dict) Dictionary containing processed folder name and processing status.
            Example: {"status": "SUCCESS", "folder_id": "T60253001_A", ...}
    """
    # Initialize high-performance thread-isolated print registry array
    console_msgs = []

    # Start timer for this video for diagnostic mode
    if config['diagnostic_mode']:
        iter_start = time.perf_counter()
    folder_id = str(row[config['col_folder_name']]).strip()
    start_time_ceil = str(row['start_time_ceil']).strip()

    # Initialize log block for parallel processing
    log_payload = ""

    # Input and output directories
    folder_path = os.path.join(config['input_directory'], folder_id)
    if not os.path.exists(folder_path):
        log_payload += f"SKIP: Folder {folder_path} not found.\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "Video folder path not found", "log_payload": log_payload, "console_msgs": console_msgs}
        
    output_path = os.path.join(
        config['output_directory'], f"{folder_id}{config['video_extension']}"
        )

    # Check for local existence or GCP presence cache to determine skipping
    already_uploaded = False
    if config['gcp_upload'] and config['delete_local_after_upload']:
        remote_filename = f"{folder_id}{config['video_extension']}"
        if remote_inventory is not None and remote_filename in remote_inventory:
            already_uploaded = True

    if not config['reprocess'] and (os.path.exists(output_path) or already_uploaded):
        log_payload += f"Deployment {folder_id} already exists locally or on GCP bucket directory. Skipping.\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "Output video already exists", "log_payload": log_payload, "console_msgs": console_msgs}

    # Skip and log any folder listed in the CSV that doesn't exist in the input
    # directory
    if not os.path.exists(folder_path):
        log_payload += f"SKIP: Folder {folder_id} not found.\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "Folder path not found", "log_payload": log_payload, "console_msgs": console_msgs}

    # 3. GATHER & SORT FILES
    video_files = [f for f in os.listdir(folder_path) 
                    if f.upper().endswith(config['video_extension'].upper())]
    if not video_files:
        log_payload += f"SKIP: No videos in {folder_id}.\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "No matched video extension files inside directory", "log_payload": log_payload, "console_msgs": console_msgs}
    video_files.sort(key=get_gopro_sort_key)

    # 4. CALCULATE TIMELINE
    # Resolve frame rates (`source_fps` will come from video metadata)
    first_file_path = os.path.join(folder_path, video_files[0])
    
    # Prevent unhandled crashes from bad files
    try:
        _, source_fps, _, _, _ = get_video_metadata(file_path=first_file_path, ffprobe_path=ffprobe_exe)
    except Exception as e:
        log_payload += f"ERROR: Primary metadata extraction failed on first file {first_file_path} in folder {folder_id}. Details: {str(e)}\n"
        return {
            "status": "ERROR",
            "folder_id": folder_id,
            "error_type": "Metadata Corruption (FFprobe Failure)",
            "error_msg": f"Failed to parse initial file structure for '{os.path.basename(first_file_path)}'. Underlying crash: {type(e).__name__}: {str(e)}",
            "log_payload": log_payload,
            "console_msgs": console_msgs
        }

    start_time_fps = float(config['start_time_fps'])
    raw_target = str(config['output_fps']).lower()
    output_fps = source_fps if raw_target == 'auto' else float(raw_target)

    # Convert between Power Director (PD) seconds and GoPro seconds
    time_scaling = start_time_fps / source_fps
    
    # Video slice times: seek using PD frame rate-derived time, then convert to
    # actual
    pd_start_seconds = timestamp_to_seconds(timestamp_str=start_time_ceil, fps=start_time_fps)
    pd_start_seconds += (int(config['time_buffer_minutes']) * 60)
    pd_duration_seconds = int(config['video_duration_minutes']) * 60

    # Prevent ffmpeg from skipping first frame due to floating-point rounding:
    #   -> 0.2 frame pullback to ensure start frame inclusion
    #   -> 0.1s trailing padding buffer to prevent truncation of final frames
    nudge = 0.2 / start_time_fps
    padding = 0.1
    start_seconds = (pd_start_seconds - nudge) * time_scaling
    video_duration_sec = (pd_duration_seconds * time_scaling)
    end_seconds = start_seconds + video_duration_sec + nudge + padding

    # Extract video metadata
    if config['diagnostic_mode']:
        console_msgs.append(f"  > Probing metadata for {len(video_files)} video chapters in {folder_id}...")
    file_data = []
    for f in video_files:
        full_p = os.path.join(folder_path, f)
        
        # Intercept bad chapters and return structural context
        try:
            dur, source_fps, br, w, h = get_video_metadata(file_path=full_p, ffprobe_path=ffprobe_exe)
        except Exception as e:
            log_payload += f"ERROR: Metadata extraction failed on file {full_p}. Details: {str(e)}\n"
            return {
                "status": "ERROR",
                "folder_id": folder_id,
                "error_type": "Metadata Corruption (FFprobe Failure)",
                "error_msg": f"Failed to parse intermediate chapter file components for '{f}'. Structural data is likely missing or corrupt. Underlying crash: {type(e).__name__}: {str(e)}",
                "log_payload": log_payload,
                "console_msgs": console_msgs
            }

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
        console_msgs.append("  > Determining needed files and trim points...")
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

    # VIDEO DURATION CHECK
    if config['skip_partial_videos']:
        # Sum the actual calculated clip durations of the matched segments
        total_clipped_seconds = sum(f_info['t'] for f_info in needed_files)
        expected_seconds = pd_duration_seconds * time_scaling
        
        # Allow a small 2-second buffer for floating-point rounding across file seams
        if total_clipped_seconds < (expected_seconds - 2.0):
            log_payload += (
                f"SKIP: {folder_id} - Insufficient footage. Found only "
                f"{total_clipped_seconds / 60:.2f} mins out of required {config['video_duration_minutes']} mins.\n"
            )
            return {
                "status": "SKIP", 
                "folder_id": folder_id, 
                "reason": "Insufficient video footage", 
                "log_payload": log_payload,
                "console_msgs": console_msgs
            }

    # If no footage matches the 24-minute window, log and skip
    if not needed_files:
        log_payload += f"SKIP: {folder_id} - No footage found for the requested time window.\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "No overlapping footage found within clipping window", "log_payload": log_payload, "console_msgs": console_msgs}

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
        v_label = f"[v{i}]"
        a_label = f"[a{i}]"
        filter_complex_parts.append(
            f"[{i}:v]trim=start={f_info['ss']}:duration={f_info['t']},"
            f"setpts=PTS-STARTPTS{v_label}"
        )
        filter_complex_parts.append(
            f"[{i}:a]atrim=start={f_info['ss']}:duration={f_info['t']},"
            f"asetpts=PTS-STARTPTS{a_label}"
        )
        filter_inputs += f"{v_label}{a_label}"

    # Target bitrate based on original GoPro metadata to ensure visual
    # fidelity
    target_bitrate = f"{int(cumulative_size * 8 / (pd_duration_seconds * time_scaling))}"
    is_auto_mode = str(config['quality_crf']).lower() == 'auto'
    if is_auto_mode and config['diagnostic_mode']:
        console_msgs.append(f"  > Targeting bitrate {int(target_bitrate)/1_000_000:.2f} Mbps to match source density.")

    # Build the filter string: e.g., `[0:v][1:v]concat=n=2:v=1[outv]`:
    fps_logic = f"fps=fps={output_fps}:round=near"
    if raw_target != 'auto':
        fps_logic += f",setpts=N/({output_fps}*TB)"

    concat_part = (
        f"{filter_inputs}concat=n={len(needed_files)}:v=1:a=1[v_stitched][outa]"
    )
    filter_complex_parts.append(concat_part)
    filter_complex_parts.append(f"[v_stitched]{fps_logic}[outv]")
    maparg = "[outv]"

    # Join all filter parts with semicolons
    filter_str = "; ".join(filter_complex_parts)

    # 6. GENERATE QC TABLE
    cumulative_output_frames = 0
    target_total_frames = int(pd_duration_seconds * start_time_fps)
    table_lines = []
    table_lines.append(f"\n{'='*80}")
    table_lines.append(f"{'QC SEAM INSPECTION TABLE - Folder: ' + folder_id + ' (' + str(config['video_duration_minutes']) + ' min)':^80}")
    table_lines.append(f"{'='*80}")
    table_lines.append(f"{'NEW VIDEO TIME':<18} | {'ACTION':<17} | {'SOURCE FILE':<17} | {'SOURCE TIMESTAMP'}")
    table_lines.append(f"{'-'*19}|{'-'*19}|{'-'*19}|{'-'*20}")

    for i, segment in enumerate(needed_files):
        start_ts = seconds_to_timestamp(cumulative_output_frames / start_time_fps, start_time_fps)
        report_ss = segment['ss'] + (nudge * time_scaling if i == 0 else 0)
        source_start = seconds_to_timestamp(report_ss / time_scaling, start_time_fps)
        table_lines.append(f"{start_ts:<18} | START SEGMENT     | {os.path.basename(segment['path']):<17} | {source_start}")
        
        # Calculate discrete frames for THIS segment by removing the nudge from the count
        actual_t = segment['t'] - (nudge * time_scaling if i == 0 else 0)
        segment_frames = int(round(actual_t * segment['fps']))
        
        # Hard-cap to prevent padding 'leakage' into the table report
        if (cumulative_output_frames + segment_frames) > target_total_frames:
            segment_frames = target_total_frames - cumulative_output_frames
        
        # Last frame index
        last_frame_idx = cumulative_output_frames + segment_frames - 1
        end_ts = seconds_to_timestamp(last_frame_idx / start_time_fps, start_time_fps)
        
        # Source end
        last_frame_rel = (segment_frames - 1) / segment['fps']
        source_end = seconds_to_timestamp((report_ss + last_frame_rel) / time_scaling, start_time_fps)
        
        if i < len(needed_files) - 1:
            table_lines.append(f"{end_ts:<18} | LAST FRAME        | {os.path.basename(segment['path']):<17} | {source_end}")
            table_lines.append(f"{' '*18} |      -- SEAM --   | {' '*17} |")
        else:
            final_end_ts = seconds_to_timestamp(target_total_frames / start_time_fps, start_time_fps)
            table_lines.append(f"{final_end_ts:<18} | VIDEO END         | {os.path.basename(segment['path']):<17} | {source_end}")
        
        cumulative_output_frames += segment_frames

    table_lines.append(f"{'-'*80}")
    table_lines.append(f"TOTAL OUTPUT FRAMES: {cumulative_output_frames} / {target_total_frames}")
    table_lines.append(f"{'='*80}\n")
    full_table_str = "\n".join(table_lines)

    # Re-check free space before starting this worker's encode
    _, _, worker_free = shutil.disk_usage(config['output_directory'])
    if (worker_free // (2**30)) < config['min_gb_required'] and process:
        log_payload += f"SKIP: {folder_id} - Disk space critical ({worker_free // (2**30)}GB left).\n"
        return {"status": "SKIP", "folder_id": folder_id, "reason": "Disk space safety constraints tripped", "log_payload": log_payload, "console_msgs": console_msgs}

    # Build execution command
    if config['use_gpu']:
        # NVIDIA Hardware Accelerated (NVENC) Configuration:
        #   -c:v h264_nvenc: Activates dedicated NVIDIA GPU hardware encoding
        #       chips.
        #   -rc vbr: Enforces Variable Bitrate control method to match
        #       structural density adjustments.
        encoder_args = ["-c:v", "h264_nvenc", "-rc", "vbr"]
        if is_auto_mode:
            # Automatic bitrate targeting matching source file structural
            # characteristics:
            #   -b:v: Dictates average overall targeted stream bitrate matching
            #       the composite inputs.
            #   -maxrate: Sets maximum allowable ceiling tolerance spike bounds
            #       for complex frames.
            #   -bufsize: Determines rate control calculation interval buffer
            #       sizing window.
            encoder_args += ["-b:v", target_bitrate, "-maxrate", "100M", "-bufsize", "100M"]
        else:
            # Constant Quality mode using hardware target scales:
            #   -b:v 0: Instructs NVENC rate control to bypass traditional
            #       explicit bitrate targets.
            #   -cq: Sets absolute hardware constant quality level threshold
            #       metric parameters.
            encoder_args += ["-b:v", "0", "-cq", str(config['quality_crf'])]
        #   -preset p7: Employs highest quality multi-pass visual optimization
        #       setting on NVIDIA chips.
        encoder_args += ["-preset", "p7"]
    else:
        # Standard Software CPU (x264) Configuration:
        #   -c:v libx264: Activates the industry-standard software H.264 video
        #       compression library.
        encoder_args = ["-c:v", "libx264"]
        if is_auto_mode:
            #   -b:v: Assigns calculated average destination bitrate
            #       constraints to match density values.
            encoder_args += ["-b:v", target_bitrate]
        else:
            #   -crf: Constant Rate Factor balancing subjective visual quality
            #        metrics against size (lower is higher quality).
            encoder_args += ["-crf", str(config['quality_crf'])]
        #   -preset medium: Provides optimized equilibrium balancing compute
        #       time against compression output curves.
        encoder_args += ["-preset", "medium"]
    
    # Video duration for metadata
    final_metadata_t = pd_duration_seconds if raw_target != 'auto' else (pd_duration_seconds * time_scaling)

    # See https://ffmpeg.org/ffmpeg.html for explicit sub-argument
    # specifications:
    #   -y: Overwrite matching existing target output files without prompting.
    #   input_args: Dynamic sequence array containing absolute paths to
    #       identified chapters (-i paths).
    #   -filter_complex: Pass consolidated filtergraph string defining trim
    #       offsets, seam stitches, and text.
    #   -map: Instruct engine to route the final custom multi-stitched video
    #       stream to the output.
    #   -map [outa]: Route the synchronized and stitched audio stream to the output file container.
    #   encoder_args: Compression configuration settings designated for CPU
    #       (libx264) or GPU hardware context.
    #   -c:a aac: Specify the Advanced Audio Coding (AAC) codec for the stitched audio stream tracking.
    #   -r: Force constant target frames per second metrics for tracking
    #       compatibility parameters.
    #   -fps_mode cfr: Force constant baseline frame mapping to override
    #       hardware timing variances.
    #   -video_track_timescale: Set custom navigation tickrate (30000) for
    #       clean frame-seeking performance.
    #   -t: Limit absolute tracking duration data to requested survey window
    #       constraints.
    cmd = [
        ffmpeg_exe, "-y"
    ] + input_args + [
        "-filter_complex", filter_str,
        "-map", maparg,
        "-map", "[outa]"
    ] + encoder_args + [
        "-c:a", "aac",
        "-r", str(output_fps),
        "-fps_mode", "cfr",
        "-video_track_timescale", "30000",
        "-t", str(final_metadata_t),
        output_path
    ]

    # 7. EXECUTION WITH AUTO-RETRY LOOP
    result = MockResult()
    if process:
        max_attempts = int(config['max_retries']) + 1
        timeout_val = int(config['timeout_minutes']) * 60 if config['timeout_minutes'] else None
        
        for attempt in range(1, max_attempts + 1):
            try:
                result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_val)
                break 
            except subprocess.TimeoutExpired:
                log_payload += f"TIMEOUT NOTICE: Deployment {folder_id} timed out on attempt {attempt}/{max_attempts}.\n"
                if attempt == max_attempts:
                    return {
                        "status": "ERROR", 
                        "folder_id": folder_id, 
                        "error_type": "FFmpeg Timeout", 
                        "error_msg": f"Execution halted after timing out consistently across {max_attempts} distinct attempts.",
                        "log_payload": log_payload,
                        "console_msgs": console_msgs
                    }
                time.sleep(5) 
                continue
                
        if result.returncode != 0:
            log_payload += f"ERROR in {folder_id}: {result.stderr}\n"
            return {
                "status": "ERROR", 
                "folder_id": folder_id, 
                "error_type": "FFmpeg Fatal Exit Code", 
                "error_msg": result.stderr.strip().split('\n')[-1],
                "log_payload": log_payload,
                "console_msgs": console_msgs
            }
    
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
            console_msgs.append(f"  > Created {folder_id}{config['video_extension']} in {iter_duration/60:.2f} minutes.\n")
        else:
            console_msgs.append(f"  > Created {folder_id}{config['video_extension']} in {iter_duration:.2f} seconds.\n")
        console_msgs.append("    Output file metrics versus expectations:\n")
        console_msgs.append(expectations_table_str)

    # Check expectations
    is_bpp_ideal_80 = avg_bpp_src * 0.80 <= actual_bpp <= avg_bpp_src * 1.20
    is_size_ideal_80 = cumulative_size * 0.80 <= actual_size <= cumulative_size * 1.20
    is_bpp_ideal_90 = avg_bpp_src * 0.90 <= actual_bpp <= avg_bpp_src * 1.10
    is_size_ideal_90 = cumulative_size * 0.90 <= actual_size <= cumulative_size * 1.10

    # Append evaluation summaries safely directly into the local string payload
    log_payload += f"\nSUMMARY OF FOLDER {folder_id}:\n"
    log_payload += f"    Estimated output video size without visual quality loss: {cumulative_size / (2**30):.2f} GB\n"
    log_payload += f"    Estimated target bitrate to maintain visual fidelity: {int(target_bitrate)/1_000_000:.2f} Mbps\n"
    log_payload += f"    Average information density of original videos: {avg_bpp_src:.4f} bits per pixel (BPP)\n"
    log_payload += ' '*30 + '* '*10 + '\n\n'
    log_payload += f"    Output video file size: {actual_size / (2**30):.2f} GB\n"
    log_payload += f"    Output video bitrate:   {actual_bitrate/1_000_000:.2f} Mbps\n"
    log_payload += f"    Information density:    {actual_bpp:.4f} BPP\n\n"
    log_payload += f"    -> Within 80% of original average BPP:  {'YES' if is_bpp_ideal_80 else 'NO  X'}\n"
    log_payload += f"    -> Within 80% of estimated file size:   {'YES' if is_size_ideal_80 else 'NO  X'}\n"
    log_payload += f"    -> Within 90% of original average BPP:  {'YES' if is_bpp_ideal_90 else 'NO  X'}\n"
    log_payload += f"    -> Within 90% of estimated file size:   {'YES' if is_size_ideal_90 else 'NO  X'}\n"

    # Centralized multi-line notes and warnings
    quality_note = ""
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
        log_payload += f"{warning_90}\n{quality_note}\n"
    elif not all([is_bpp_ideal_80, is_size_ideal_80]):
        log_payload += f"{warning_80}\n{quality_note}\n"

    # GCP high-speed pipeline upload block
    upload_status = "PENDING"
    throughput_rate = "Unknown"
    console_msg = ""
    upload_speed_mibs = 0.0  # Initialize a raw float tracking variable

    if config['gcp_upload'] and process:
        try:
            gcloud_exec = shutil.which("gcloud")
            cmd_up = [gcloud_exec, "storage", "cp", output_path, config['gcp_bucket_path']]
            
            result_up = subprocess.run(cmd_up, capture_output=True, text=True)
            
            if result_up.returncode == 0:
                upload_status = "SUCCESS"
                
                # Parse out the final throughput summary from the buffered stderr block
                for out_line in reversed(result_up.stderr.splitlines()):
                    if "throughput" in out_line.lower():
                        throughput_rate = out_line.split(":")[-1].strip()
                        
                        # Extract the numeric float value and normalize units for the average calculation
                        spd_match = re.search(r'([0-9.]+)\s*([a-zA-Z/]+)', throughput_rate)
                        if spd_match:
                            val = float(spd_match.group(1))
                            unit = spd_match.group(2).lower()
                            if 'k' in unit:
                                upload_speed_mibs = val / 1024.0  # Normalize KiB to MiB
                            elif 'b' in unit and 'm' not in unit:
                                upload_speed_mibs = val / (1024.0 * 1024.0)  # Normalize Bytes to MiB
                            else:
                                upload_speed_mibs = val  # Already MiB/s or MB/s
                        break
                
                console_msg = f"  > {folder_id} uploaded to GCP at {throughput_rate}"
                if config['delete_local_after_upload']:
                    os.remove(output_path)
            else:
                upload_status = f"FAILED: {result_up.stderr.strip()}"                
        
        except Exception as e:
            upload_status = f"FAILED: {str(e)}"

    if result.returncode == 0:
        log_payload += full_table_str
        if config['diagnostic_mode']:
            console_msgs.append(full_table_str)

    # Return the entire un-clipped string package smoothly to the parent thread
    return {
        "status": "SUCCESS", 
        "folder_id": folder_id, 
        "upload_status": upload_status, 
        "output_path": output_path,
        "targets": {
            "bpp_80": is_bpp_ideal_80, "size_80": is_size_ideal_80,
            "bpp_90": is_bpp_ideal_90, "size_90": is_size_ideal_90
        },
        "log_payload": log_payload,
        "console_msgs": console_msgs,
        "upload_speed": upload_speed_mibs
    }

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
    config['max_retries'] = config.get('max_retries', 2)
    config['min_gb_required'] = config.get('min_gb_required', 10)
    config['num_workers'] = config.get('num_workers', 1)
    config['output_fps'] = config.get('output_fps', 'auto')
    config['time_buffer_minutes'] = config.get('time_buffer_minutes', -2)
    config['quality_crf'] = config.get('quality_crf', 'auto')
    config['reprocess'] = config.get('reprocess', False)
    config['skip_partial_videos'] = config.get('skip_partial_videos', True)
    config['start_time_fps'] = config.get('start_time_fps', 30)
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
        print(f"\n[!] FATAL ERROR: Insufficient disk space ({free // (2**30)}GB remaining). Stopping.")
        return False
    
    # Confirm GCP bucket authentication
    if config['gcp_upload']:
        gcloud_exec = shutil.which("gcloud")
        if 'gcp_bucket_path' not in config:
            print("\nERROR: `gcp_upload` set to True but no GCP bucket was defined.")
            return False
        if not check_gcp_auth(bucket_path=config['gcp_bucket_path']):
            return False

    # Initialize the log file and write current configuration parameters
    mode = "w" if config['clear_log'] else "a"
    with open(config['log_file'], mode) as log:
        log.write(f"{'#'*80}\nSESSION START: {datetime.now()}\n")
        log.write(f"CONFIGURATION: {config_path}\n\n")
        for key, value in config.items():
            log.write(f"  -> {key}: {value}\n")
        log.write("\n")
        log.write(f"{'#'*80}\n\n")

    # Initialize a standalone runtime state variable for our file lookup cache
    remote_inventory = None

    # Map bucket folder components once to protect worker pipelines
    if config['gcp_upload'] and config['delete_local_after_upload']:
        print("  > Mapping remote bucket directory to cache existing deployments...", flush=True)
        ls_remote = subprocess.run(
            [gcloud_exec, "storage", "ls", config['gcp_bucket_path']],
            capture_output=True, text=True
        )

        remote_inventory_set = set()
        if ls_remote.returncode == 0:
            for line in ls_remote.stdout.splitlines():
                filename = line.strip().split('/')[-1]
                if filename:
                    remote_inventory_set.add(filename)
        remote_inventory = remote_inventory_set

    # Load and format CSV
    try:
        df = pd.read_csv(config['csv_path'], encoding='utf-8')
    except UnicodeDecodeError:
        df = pd.read_csv(config['csv_path'], encoding='ISO-8859-1')
    df.columns = df.columns.str.strip()
    df[config['col_folder_name']] = df[config['col_folder_name']].str.strip()
    df['start_time_ceil'] = df[config['col_start_time']].apply(time_ceiling)

    print(f"  > Processing {int(df.shape[0])} deployments. Monitoring progress...\n", flush=True)
    tasks = df.to_dict('records')
    failed_uploads, overall_success = [], True

    # Multi-threaded batch execution tracking registries
    errors_by_type = defaultdict(list)
    missed_metrics = defaultdict(list)
    skipped_deployments = defaultdict(list)
    success_count = 0

    # Total progress bar
    custom_format = "{desc}: {percentage:3.0f}%|{bar}| {n_fmt}/{total_fmt} [{postfix}]"
    pbar = tqdm(total=len(tasks), position=0, desc="Total Progress", bar_format=custom_format)
    pbar.set_postfix_str("Avg Upload: N/A")

    # Initialize a list to hold successful network metrics across parallel returns
    successful_upload_speeds = []

    try:
        with ProcessPoolExecutor(max_workers=config['num_workers']) as executor:
            futures = {}
            for task_idx, row in enumerate(tasks, start=1):
                future = executor.submit(
                    process_single_deployment, 
                    row, config, ffmpeg_exe, ffprobe_exe, 
                    process, remote_inventory
                )
                futures[future] = row

            for future in as_completed(futures):
                result = future.result()
                
                # Write any console outputs safely through the parent process tqdm wrapper
                if result.get('console_msgs'):
                    for msg in result['console_msgs']:
                        tqdm.write(msg)
                        
                pbar.update(1)
                folder_id = result['folder_id']
                
                # Dynamic performance evaluation updates
                if result.get('upload_speed') and result['upload_speed'] > 0:
                    successful_upload_speeds.append(result['upload_speed'])
                    running_avg = sum(successful_upload_speeds) / len(successful_upload_speeds)
                    pbar.set_postfix_str(f"Avg Upload: {running_avg:.1f} MiB/s")
                
                # Write log
                if result.get('log_payload'):
                    with open(config['log_file'], "a") as log:
                        log.write(result['log_payload'])
                
                # Non-blocking collection of worker process hard termination errors
                if result['status'] == "ERROR":
                    err_type = result.get('error_type', 'Unclassified Functional Error')
                    err_msg = result.get('error_msg', 'No trace log strings provided.')
                    errors_by_type[err_type].append(f"{folder_id} -> {err_msg}")
                    overall_success = False
                    
                elif result['status'] == "SKIP":
                    reason = result.get('reason', 'Skipped')
                    skipped_deployments[reason].append(folder_id)
                    
                elif result['status'] == "SUCCESS":
                    success_count += 1
                    t_flags = result.get('targets', {})
                    missed_list = []
                    if not t_flags.get('bpp_80'):
                        missed_list.append("Missed 80% BPP target")
                    if not t_flags.get('size_80'):
                        missed_list.append("Missed 80% file size target")
                    if not t_flags.get('bpp_90'):
                        missed_list.append("Missed 90% BPP target")
                    if not t_flags.get('size_90'):
                        missed_list.append("Missed 90% file size target")
                    
                    if missed_list:
                        missed_metrics[folder_id] = missed_list

                    # Cleanly collect upload statuses only when GCP routines are
                    # globally enabled
                    if config['gcp_upload'] and result.get('upload_status') and not result['upload_status'] == "SUCCESS":
                        failed_uploads.append(result['output_path'])
    
    except KeyboardInterrupt:
        # CONCURRENT PROCESS TREE CLEANUP GUARD
        print("\n\n[!] USER ABORT DETECTED: Killing all video processing and cloud upload channels immediately...")
        pbar.close()
        
        # Prevent remaining queued items from running and clear the pipeline pool
        executor.shutdown(wait=False, cancel_futures=True)
        
        # Execute a platform-specific tree-nuke to ensure no orphan background FFmpeg processes leak
        if sys.platform.startswith("win"):
            # Windows: Forcefully terminate the process tree (/T) matching this script's PID
            subprocess.run(["taskkill", "/F", "/T", "/PID", str(os.getpid())], capture_output=True)
        else:
            # Mac/Linux: Signal the entire process group hierarchy to terminate instantly
            import signal
            try:
                os.killpg(os.getpgrp(), signal.SIGKILL)
            except ProcessLookupError:
                pass
        sys.exit(1)

    pbar.close()

    # 2. UNIFIED FINAL LEDGER MASTER RECORD BLOCK
    summary_lines = []
    summary_lines.append(f"\n{'='*80}\n{'FINAL BATCH PROCESSING EXECUTION SUMMARY REPORT':^80}\n{'='*80}")
    summary_lines.append(f"Successfully processed deployments: {success_count} / {len(tasks)}")
    summary_lines.append(f"Number of skipped deployments: {sum(len(v) for v in skipped_deployments.values())}")
    summary_lines.append(f"Number of hard failures encountered:  {sum(len(v) for v in errors_by_type.values())}\n")

    if skipped_deployments:
        summary_lines.append(f"{'-'*18} SKIPPED FILES {'-'*18}")
        for skip_reason, list_folders in skipped_deployments.items():
            summary_lines.append(f"  -> {skip_reason}: {len(list_folders)} deployments affected.")

    if errors_by_type:
        summary_lines.append(f"\n{'!'*15} ERRORS ENCOUNTERED {'!'*15}")
        for err_title, deployments in errors_by_type.items():
            summary_lines.append(f"\n * Error Category: {err_title} ({len(deployments)} deployments affected):")
            for d in deployments:
                summary_lines.append(f"   -> {d}")

    if missed_metrics:
        summary_lines.append(f"\n{'-'*10} QUALITY THRESHOLD (80% / 90%) MISSES {'-'*10}")
        summary_lines.append(f"Total Folders with Quality Variations: {len(missed_metrics)}")
        for idx, (f_id, faults) in enumerate(missed_metrics.items(), start=1):
            summary_lines.append(f"  -> Deployment {f_id}: {', '.join(faults)}")

    summary_lines.append(f"\n{'='*80}\n")
    master_summary_str = "\n".join(summary_lines)

    # Output metrics directly to file and terminal
    with open(config['log_file'], "a") as log:
        log.write(master_summary_str)
    print(master_summary_str)

    # Guard against dry runs, disabled uploads, and handle local cleanup
    if process and config['gcp_upload'] and failed_uploads:
        gcloud_exec = shutil.which("gcloud")
        for path in failed_uploads:
            retry = subprocess.run([gcloud_exec, "storage", "cp", path, config['gcp_bucket_path']])
            if retry.returncode == 0:
                # If the recovery upload succeeded, check if we need to delete
                # the local copy
                if config['delete_local_after_upload'] and os.path.exists(path):
                    os.remove(path)
            else:
                overall_success = False

    return (overall_success, os.path.basename(config['log_file']))

if __name__ == "__main__":
    # Parse command-line arguments
    args = parse_args()

    # Script timer
    script_start = time.perf_counter()

    # Launch the script and sweep through deployments
    success, log = process_deployments(
        config_path=args.config_path, process=args.process
    )
    
    # Calculate total script processing runtime duration
    script_duration = time.perf_counter() - script_start
    if script_duration > 60:
        runtime = f"{script_duration/60:.2f} minutes"
    else:
        runtime = f"{script_duration:.2f} seconds"
        
    status_msg = "SUCCESSFULLY COMPLETED" if success else "COMPLETED WITH FUNCTIONAL ERRORS"
    
    print(f"\n{status_msg}")
    print(f"Total overall runtime: {runtime}.")
    print(f"Review '{log}' for detailed metrics.", flush=True)