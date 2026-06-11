"""
Cloud upload file check
-----------------------
A file inspection utility that compares file names and sizes between the input (source) directory and the Google Cloud Project (GCP) storage bucket using the same configuration YAML file as the original script.

Usage:
    python cloud-upload-check.py path/to/name-of-configuration-file.yml

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

from urllib.parse import urlparse
import subprocess
import yaml
import sys
import os
import csv
import shutil
import argparse

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
    return parser.parse_args()

def load_config(config_path: str = 'configurations.yml'):
    """Loads and verifies the YAML configuration file.
    
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

    # Validate keys
    REQUIRED_KEYS = {'output_directory', 'gcp_bucket_path'}
    missing = [f"  - '{k}'" for k in REQUIRED_KEYS if k not in config]
    if missing:
        error_msg = (
            "\n[!] CONFIGURATION ERROR: Missing required keys in YAML:\n" +
            "\n".join(missing) + "\n\n"
        )
        raise ValueError(error_msg)
    config['video_extension'] = config.get('video_extension', '.MP4')

    return config

def extract_gcp_prefix(bucket_path):
    """
    Safely extracts the folder prefix from a GCP bucket path, automatically
    stripping out wildcards, file names, or extensions.

    Arguments
    ---------
    bucket_path (str): full path to GCP storage bucket and folder

    Returns
    -------
    Returns the file prefix only
    """
    # Parse the gs:// URI safely (separates bucket from path)
    # urlparse("gs://my-bucket/folder/*.MP4").path -> "/folder/*.MP4"
    parsed_path = urlparse(bucket_path).path
    
    # Strip the leading slash left over by urlparse
    prefix = parsed_path.lstrip('/')
    
    # Return with a trailing slash, or empty string if it's the bucket root
    return f"{prefix.rstrip('/')}/" if prefix else ""

def get_cloud_manifest(bucket_path, extension=None):
    """Queries GCP bucket and returns data in a dictionary.
    
    Arguments
    ---------
    bucket_path (str): full path to GCP storage bucket and folder to check
    extension (str): file extension to filter by (optional)

    Returns
    -------
    dict of file names and sizes 
    
    """
    bucket_path = f"{bucket_path.rstrip('/*')}/*"
    print(f"Fetching cloud bucket inventory from {bucket_path}...")
    # Use gcloud CLI to retrieve names and sizes of all objects in the bucket
    # Use shutil.which to find the actual path of gcloud (handles .cmd on Windows)
    gcloud_exec = shutil.which("gcloud")
    
    if not gcloud_exec:
        print("\nERROR: 'gcloud' command not found. Is Google Cloud SDK installed and in your PATH?")
        return False
    cmd = [
        gcloud_exec, "storage", "objects", "list", 
        bucket_path, 
        '--format=csv[no-heading](name, size)'
    ]
    
    # Run gcloud and capture stdout directly into memory
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, check=True)
    except subprocess.CalledProcessError as e:
        print(f"\n❌ ERROR running gcloud command: {e.stderr}")
        sys.exit(1)
    except FileNotFoundError:
        print("\n❌ ERROR: 'gcloud' CLI tool not found. Make sure it's installed and in your PATH.")
        sys.exit(1)

    # Parse the text string using Python's csv reader and store as dict
    cloud_data = {}
    ext_lower = f".{extension.lstrip('.').lower()}" if extension else None
    reader = csv.reader(result.stdout.strip().splitlines())
    for row in reader:
        if row:
            path, size = row[0].strip(), row[1].strip()
        
            if not ext_lower or path.lower().endswith(ext_lower):
                cloud_data[path] = int(size)
            
    return cloud_data

def get_local_manifest(local_path, prefix, extension=None):
    """Scans the local top-level directory and returns data in a dictionary.
    
    Arguments
    ---------
    local_path (str): local directory to compare to cloud
    prefix (str): prefix appended to file name in the storage bucket
    extension (str): file extension to filter by (optional)
    """
    print(f"Scanning local files in {local_path}...")
    
    if not os.path.exists(local_path):
        print(f"\n❌ ERROR: Cannot access local path: {local_path}")
        sys.exit(1)

    ext_lower = extension.lower() if extension else None    
    local_data = {}
    for file in os.listdir(local_path):
        full_path = os.path.join(local_path, file)
        
        if os.path.isfile(full_path) and (not ext_lower or file.lower().endswith(ext_lower)):
            try:
                size = os.path.getsize(full_path)
                # Format to match the cloud path structure
                gcp_style_path = f"{prefix}{file}".replace("//", "/")
                local_data[gcp_style_path] = size
            except Exception as e:
                print(f"  ⚠️ Error reading size for {file}: {e}")
                
    return local_data

def compare_inventories(local, cloud):
    """Compares local file names and sizes with those in the cloud and prints
    results.
    
    Arguments
    ---------
    local (dict): dictionary containing `file_name: file_size` pairs for all
        local files
    cloud (dict): dictionary containing `file_name: file size` pairs for all
        cloud files    
    """

    print("\nAnalyzing discrepancies...")
    
    # Check for missing files in either location
    missing_in_cloud = [p for p in local if p not in cloud]
    missing_in_local = [p for p in cloud if p not in local]

    # Check for size mismatches
    size_mismatches = []
    for p, local_size in local.items():
        if p in cloud and cloud[p] != local_size:
            size_mismatches.append((p, local_size, cloud[p]))
    extension = p.split('.')[-1]

    print("\n=====================================")
    print("        COMPARISON RESULTS           ")
    print("=====================================")
    print(f"Local drive count: {len(local)} {extension} files")
    print(f"GCP Bucket count:  {len(cloud)} {extension} objects")
    print("-------------------------------------")

    if not missing_in_cloud and not missing_in_local and not size_mismatches:
        print("🎉 SUCCESS! Every file matches perfectly in name and size.")
    else:
        if missing_in_cloud:
            print(f"❌ Missing in Cloud ({len(missing_in_cloud)}):")
            for item in missing_in_cloud[:10]:
                print(f"  - {item}")
            if len(missing_in_cloud) > 10:
                print(f"  ... and {len(missing_in_cloud)-10} more.")
            
        if missing_in_local:
            print(f"⚠️ Extra in Cloud / Missing Locally ({len(missing_in_local)}):")
            for item in missing_in_local[:10]:
                print(f"  - {item}")
            if len(missing_in_local) > 10:
                print(f"  ... and {len(missing_in_local)-10} more.")
            
        if size_mismatches:
            print(f"❌ Byte Size Mismatches ({len(size_mismatches)}):")
            for item, l_sz, c_sz in size_mismatches[:10]:
                print(f"  - {item} (Local: {l_sz} bytes, Cloud: {c_sz} bytes)")
            if len(size_mismatches) > 10:
                print(f"  ... and {len(size_mismatches)-10} more.")

if __name__ == "__main__":
    # Load configuration file
    args = parse_args()
    config = load_config(args.config_path)

    # Fetch cloud and local inventory
    cloud_inventory = get_cloud_manifest(
        bucket_path=config['gcp_bucket_path'],
        extension=config['video_extension']
        )
    prefix = extract_gcp_prefix(config['gcp_bucket_path'])
    local_inventory = get_local_manifest(
        local_path=config['output_directory'],
        prefix=prefix,
        extension=config['video_extension']
        )
    
    # Compare and Print
    compare_inventories(local=local_inventory, cloud=cloud_inventory)