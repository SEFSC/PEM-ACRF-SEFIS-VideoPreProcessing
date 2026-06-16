#' Cloud upload file check (R Version)
#' -----------------------------------
#' A file inspection utility that compares file names and sizes between the
#' input (source) directory and the Google Cloud Project (GCP) storage bucket
#' using the same configuration YAML file as the original script.
#'
#' Author:  Southeast Fishery Independent Survey (SEFIS)
#' Version: 2026.1.1

# Ensure the required yaml package is available
if (!requireNamespace("yaml", quietly = TRUE)) {
  stop("The 'yaml' package is required. Please install it using: install.packages('yaml')")
}

# =============================================================================
# HELPER FUNCTIONS AND METHODS
# =============================================================================

parse_args <- function() {
  #' Parses command-line arguments or prompts the user via a file dialog window.
  args <- commandArgs(trailingOnly = TRUE)
  
  # If run via command line with an argument, use that argument
  if (length(args) > 0) {
    return(args[1])
  }
  
  # If run interactively (RStudio/Positron/Console), pop up a file explorer
  if (interactive()) {
    cat("\n[➔] A file dialog has opened. Please select your configuration YAML file...\n")
    flush.console() # Force text to display before the window blocks the console
    
    config_path <- tryCatch({
      file.choose()
    }, error = function(e) {
      cat("\n⚠️ File selection cancelled by user. Attempting default 'configurations.yml'...\n")
      return("configurations.yml")
    })
    
    return(config_path)
  }
  
  # Fallback default for headless/non-interactive environments
  return("configurations.yml")
}

load_config <- function(config_path = "configurations.yml") {
  #' Loads and verifies the YAML configuration file.
  if (!file.exists(config_path)) {
    stop(sprintf("\n[!] ERROR: Configuration file not found at '%s'\n", config_path))
  }
  
  config <- suppressWarnings(yaml::read_yaml(config_path))
  
  # Validate keys
  required_keys <- c("output_directory", "gcp_bucket_path")
  missing <- required_keys[!(required_keys %in% names(config))]
  
  if (length(missing) > 0) {
    missing_formatted <- paste(sprintf("  - '%s'", missing), collapse = "\n")
    stop(sprintf("\n[!] CONFIGURATION ERROR: Missing required keys in YAML:\n%s\n\n", missing_formatted))
  }
  
  if (is.null(config$video_extension)) {
    config$video_extension <- ".MP4"
  }
  
  return(config)
}

extract_gcp_prefix <- function(bucket_path) {
  #' Safely extracts the folder prefix from a GCP bucket path.
  prefix <- gsub("^gs://[^/]+/", "", bucket_path)
  prefix <- gsub("^/", "", prefix) 
  
  if (nchar(prefix) > 0) {
    clean_prefix <- gsub("[/*]+$", "", prefix)
    return(paste0(clean_prefix, "/"))
  } else {
    return("")
  }
}

get_cloud_manifest <- function(bucket_path, extension = NULL) {
  #' Queries GCP bucket using gcloud CLI and returns data in a named list.
  clean_path <- gsub("[/*]+$", "", bucket_path)
  gcloud_target_path <- paste0(clean_path, "/*")
  
  cat(sprintf("Fetching cloud bucket inventory from %s...\n", gcloud_target_path))
  
  gcloud_exec <- Sys.which("gcloud")
  if (gcloud_exec == "") {
    cat("\nERROR: 'gcloud' command not found. Is Google Cloud SDK installed and in your PATH?\n")
    return(FALSE)
  }
  
  cmd_args <- c(
    "storage", "objects", "list", 
    gcloud_target_path, 
    "--format=csv[no-heading](name,size)"
  )
  
  result <- system2(gcloud_exec, args = cmd_args, stdout = TRUE, stderr = TRUE)
  
  status <- attr(result, "status")
  if (!is.null(status) && status != 0) {
    cat(sprintf("\n❌ ERROR running gcloud command:\n%s\n", paste(result, collapse = "\n")))
    stop(status = 1)
  }
  
  cloud_data <- list()
  if (length(result) == 0 || (length(result) == 1 && result == "")) {
    return(cloud_data)
  }
  
  ext_lower <- if (!is.null(extension)) tolower(sub("^\\.*", ".", extension)) else NULL
  
  con <- textConnection(result)
  df <- tryCatch(
    read.csv(con, header = FALSE, stringsAsFactors = FALSE, colClasses = c("character", "numeric")),
    error = function(e) NULL
  )
  close(con)
  
  if (is.null(df) || nrow(df) == 0) return(cloud_data)
  
  for (i in 1:nrow(df)) {
    path <- trimws(df[i, 1])
    size <- df[i, 2]
    
    if (is.null(ext_lower) || endsWith(tolower(path), ext_lower)) {
      cloud_data[[path]] <- size
    }
  }
  
  return(cloud_data)
}

get_local_manifest <- function(local_path, prefix, extension = NULL) {
  #' Scans the local top-level directory and returns data in a named list.
  cat(sprintf("Scanning local files in %s...\n", local_path))
  
  if (!dir.exists(local_path)) {
    cat(sprintf("\n❌ ERROR: Cannot access local path: %s\n", local_path))
    stop(status = 1)
  }
  
  all_items <- list.files(local_path, full.names = FALSE, recursive = FALSE)
  ext_lower <- if (!is.null(extension)) tolower(sub("^\\.*", ".", extension)) else NULL
  
  local_data <- list()
  for (item in all_items) {
    full_path <- file.path(local_path, item)
    
    if (!dir.exists(full_path) && (is.null(ext_lower) || endsWith(tolower(item), ext_lower))) {
      tryCatch({
        size <- file.info(full_path)$size
        
        gcp_style_path <- paste0(prefix, item)
        gcp_style_path <- gsub("//", "/", gcp_style_path)
        
        local_data[[gcp_style_path]] <- size
      }, error = function(e) {
        cat(sprintf("  ⚠️ Error reading size for %s: %s\n", item, e$message))
      })
    }
  }
  
  return(local_data)
}

compare_inventories <- function(local, cloud, video_extension) {
  #' Compares names and sizes between both environments and handles empty edges.
  cat("\nAnalyzing discrepancies...\n")
  
  local_names <- names(local)
  cloud_names <- names(cloud)
  
  missing_in_cloud <- local_names[!(local_names %in% cloud_names)]
  missing_in_local <- cloud_names[!(cloud_names %in% local_names)]
  
  size_mismatches = list()
  common_names <- local_names[local_names %in% cloud_names]
  
  for (p in common_names) {
    if (local[[p]] != cloud[[p]]) {
      size_mismatches[[p]] <- list(local = local[[p]], cloud = cloud[[p]])
    }
  }
  
  clean_ext <- gsub("^\\.*", "", video_extension)
  
  cat("\n=====================================\n")
  cat("        COMPARISON RESULTS           \n")
  cat("=====================================\n")
  cat(sprintf("Local drive count: %d %s files\n", length(local), clean_ext))
  cat(sprintf("GCP Bucket count:  %d %s objects\n", length(cloud), clean_ext))
  cat("-------------------------------------\n")
  
  if (length(missing_in_cloud) == 0 && length(missing_in_local) == 0 && length(size_mismatches) == 0) {
    cat("🎉 SUCCESS! Every file matches perfectly in name and size.\n")
  } else {
    if (length(missing_in_cloud) > 0) {
      cat(sprintf("❌ Missing in Cloud (%d):\n", length(missing_in_cloud)))
      limit <- min(10, length(missing_in_cloud))
      for (i in 1:limit) cat(sprintf("  - %s\n", missing_in_cloud[i]))
      if (length(missing_in_cloud) > 10) cat(sprintf("  ... and %d more.\n", length(missing_in_cloud) - 10))
    }
    
    if (length(missing_in_local) > 0) {
      cat(sprintf("⚠️ Extra in Cloud / Missing Locally (%d):\n", length(missing_in_local)))
      limit <- min(10, length(missing_in_local))
      for (i in 1:limit) cat(sprintf("  - %s\n", missing_in_local[i]))
      if (length(missing_in_local) > 10) cat(sprintf("  ... and %d more.\n", length(missing_in_local) - 10))
    }
    
    if (length(size_mismatches) > 0) {
      cat(sprintf("❌ Byte Size Mismatches (%d):\n", length(size_mismatches)))
      mismatch_names <- names(size_mismatches)
      limit <- min(10, length(mismatch_names))
      for (i in 1:limit) {
        p <- mismatch_names[i]
        cat(sprintf("  - %s (Local: %.0f bytes, Cloud: %.0f bytes)\n", 
                    p, size_mismatches[[p]]$local, size_mismatches[[p]]$cloud))
      }
      if (length(size_mismatches) > 10) cat(sprintf("  ... and %d more.\n", length(size_mismatches) - 10))
    }
  }
}

# =============================================================================
# MAIN RUNNER
# =============================================================================

main <- function() {
  config_path <- parse_args()
  config <- load_config(config_path)
  
  cloud_inventory <- get_cloud_manifest(
    bucket_path = config$gcp_bucket_path,
    extension = config$video_extension
  )
  
  prefix <- extract_gcp_prefix(config$gcp_bucket_path)
  
  local_inventory <- get_local_manifest(
    local_path = config$output_directory,
    prefix = prefix,
    extension = config$video_extension
  )
  
  compare_inventories(
    local = local_inventory, 
    cloud = cloud_inventory, 
    video_extension = config$video_extension
  )
}

main()