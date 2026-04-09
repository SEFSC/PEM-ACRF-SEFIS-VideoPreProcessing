@echo off
set /p config_file="Drag and drop your .yml file here and press Enter: "

:: The quotes around %config_file% handle paths with spaces safely
.venv\Scripts\python clip-and-stitch.py %config_file%

echo.
echo Processing finished.
pause