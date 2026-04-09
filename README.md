# PEM-ACRF-SEFIS-VideoPreProcessing

**A Python utility for clipping and stitching SEFIS videos for automated analysis**

* Folders are named using a combination of a project code, year, collection, and camera. E.g., `T60250001_A`. We typically call this the "collection number" for short. 
* Each folder is a single deployment of a trap with a camera. Each folder contains a board file and a number of underwater video files. 
* There is a csv file with each unique identifying collection number in a column, and the `timeonbottom` time, which is the elapsed time from when the video files start to when the trap lands on the bottom. 
* We want to clip out a segment of video starting exactly 8 minutes after the trap lands on bottom and ending 32 minutes after the trap lands on bottom, for a 24-min video clip in total. This will involve stitching files together as well. 
* This 24-min video clip should be named exactly like the folder containing the files (e.g., `T60250001_A`)

More details to come as the repository is built out. [Read the docs](https://SEFSC.github.io/PEM-ACRF-SEFIS-VideoPreProcessing/) to learn more.

## Usage

If on Windows, the easiest way to get started is to clone this repository and run the included `setup.bat` script. This will create and configure the necessary virtual environment and download and extract the required video processing tools.

To use the utility on Windows, simply run the `process_videos.bat` file. You will be prompted to select the configurations YAML file to use, and the script will then run according to the settings specified in that file. [See the docs](https://SEFSC.github.io/PEM-ACRF-SEFIS-VideoPreProcessing/usage/) for more details.

Alternatively, the `process_videos_dragndrop.bat` file will open a Windows Command Prompt terminal and prompt you to drag and drop the configurations YAML file into the terminal. Open a File Explorer window, navigate to the desired YAML file, and drag it into the terminal as instructed. The script will then run according to the settings specified in that file.

Mac or Linux users should follow either the instructions for RStudio or command line usage in the [documentation](https://SEFSC.github.io/PEM-ACRF-SEFIS-VideoPreProcessing/usage/).

## Disclaimer

This repository is a scientific product and is not official communication of the National Oceanic and Atmospheric Administration, or the United States Department of Commerce. All NOAA GitHub project code is provided on an ‘as is’ basis and the user assumes responsibility for its use. Any claims against the Department of Commerce or Department of Commerce bureaus stemming from the use of this GitHub project will be governed by all applicable Federal law. Any reference to specific commercial products, processes, or services by service mark, trademark, manufacturer, or otherwise, does not constitute or imply their endorsement, recommendation or favoring by the Department of Commerce. The Department of Commerce seal and logo, or the seal and logo of a DOC bureau, shall not be used in any manner to imply endorsement of any commercial product or activity by DOC or the United States Government.

## License

This content was created by U.S. Government employees as part of their official duties. This content is not subject to copyright in the United States (17 U.S.C. §105) and is in the public domain within the United States of America. Additionally, copyright is waived worldwide through the CC0 1.0 Universal public domain dedication.