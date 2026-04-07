# PEM-ACRF-SEFIS-VideoPreProcessing

Stitching and clipping SEFIS videos for automated analysis

- Folders are named using a combination of a project code, year, collection, and camera. E.g., `T60250001_A`. We typically call this the "collection number" for short. 

- Each folder is a single deployment of a trap with a camera. Each folder contains a board file and a number of underwater video files. 

- There is a csv file with each unique identifying collection number in a column, and the `timeonbottom` time, which is the elapsed time from when the video files start to when the trap lands on the bottom. 

- We want to clip out a segment of video starting exactly 8 minutes after the trap lands on bottom and ending 32 minutes after the trap lands on bottom, for a 24-min video clip in total. This will involve stitching files together as well. 

- This 24-min video clip should be named exactly like the folder containing the files (e.g., `T60250001_A`)

More details to come as the repository is built out.

## Set up

### Dependencies

**FFmpeg** and **FFprobe**:

1. [Download the latest essential build](https://www.gyan.dev/ffmpeg/builds/), which might be called `ffmpeg-git-essentials` or `ffmpeg-release-essentials`.

2. Once downloaded, right-click the `.zip` folder and select **Extract All...** to extract it to a location that does not need elevated admin privileges.

3. Inside the extracted folder is a subfolder called `bin` containing `ffmpeg.exe` and `ffprobe.exe`. Open this folder, right-click it in the top directory bar, and select **Copy Address as Text**. Paste this in `configurations.yml` as the directory path for `ffmpeg.exe` and `ffprobe.exe`. Be sure to retain the executable file names at the end of the path.

**Python 3.8+**

[Download](https://www.python.org/downloads/) and install Python if needed. This tool was created using Python 3.14.3.

Once Python is installed, open a terminal or command prompt in the project directory and create a virtual environment to support package management. In this example, we'll call it `.venv` but it can be called anything. Just be sure to remember its name:

```bash
python -m venv .venv
```

Activate the new virtual environment and install the packages using the accompanying `requirements.txt` file:

```bash
source .venv\Scripts\activate
pip install -r requirements.txt
```

## Usage

To use the utility, open a command prompt to the project directory containing the script and virtual environment. Activate the virtual environment:

```bash
source .venv\Scripts\activate
```

Set the configurations in the configurations YAML file as desired and call the utility, passing the name of the configuration file to use. For example:

```bash
python clip-and-stich.py configurations.yml
```

A new video file will be created according to the settings in the configuration file and a `processing_log.txt` file will be created in the current working directory alongside the script.

When finished, simply deactivate the virtual environment:

```bash
deactivate
```

## Disclaimer

This repository is a scientific product and is not official communication of the National Oceanic and Atmospheric Administration, or the United States Department of Commerce. All NOAA GitHub project code is provided on an ‘as is’ basis and the user assumes responsibility for its use. Any claims against the Department of Commerce or Department of Commerce bureaus stemming from the use of this GitHub project will be governed by all applicable Federal law. Any reference to specific commercial products, processes, or services by service mark, trademark, manufacturer, or otherwise, does not constitute or imply their endorsement, recommendation or favoring by the Department of Commerce. The Department of Commerce seal and logo, or the seal and logo of a DOC bureau, shall not be used in any manner to imply endorsement of any commercial product or activity by DOC or the United States Government.