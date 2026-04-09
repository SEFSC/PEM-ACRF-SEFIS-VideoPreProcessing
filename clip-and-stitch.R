#' @title SEFIS Video Processing Launcher
#' @description A wrapper script to execute the Python-based GoPro Clip-and-Stitch utility.
#' This script facilitates the selection of configuration files and ensures the 
#' correct Python virtual environment is utilized.
#' 
#' @author matt.grossi@@noaa.gov
#' @version 2026.0.1
#' @date 2026-04-08

# 1. Environment Configuration
# Fix: Path updated to match the hidden '.venv' folder created by setup.bat
python_path <- file.path(".venv", "Scripts", "python.exe")
script_path <- "clip-and-stitch.py"

# 2. Sanity Checks
if (!file.exists(python_path)) {
  stop("Python environment not found. Please run 'setup.bat' before using this launcher.")
}

if (!file.exists(script_path)) {
  stop("Main script 'clip-and-stitch.py' not found in the current directory.")
}

# 3. User Interaction
# Opens a standard OS file picker for the user to select their YAML configuration
message("Select your configurations.yml file...")
config_path <- file.choose(new = FALSE)

if (is.null(config_path) || config_path == "") {
  stop("No configuration file selected. Aborting.")
}

# 4. Execution
message("Starting video processing... This may take several minutes.")
message("Note: Processing maintains NTSC 29.97 fps standards for frame accuracy.")

# system2() is the preferred way to call external processes in R
# shQuote() ensures paths with spaces are handled correctly
result <- system2(python_path, 
                  args = c(script_path, shQuote(config_path)),
                  stdout = TRUE, 
                  stderr = TRUE)

# 5. Cleanup & Reporting
message("Processing Complete!")
# Optional: Display the last few lines of output for immediate feedback
cat(tail(result, 5), sep = "\n")