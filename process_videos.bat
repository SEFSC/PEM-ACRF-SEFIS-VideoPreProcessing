@echo off
setlocal
set "config_file="

echo -----------------------------------------------------------
echo SEFIS Video Utility
echo -----------------------------------------------------------

:: 1. Show the instruction Message Box first
:: [void] suppresses the numeric return value of the button click
powershell -noprofile -command "Add-Type -AssemblyName System.Windows.Forms; [void][System.Windows.Forms.MessageBox]::Show('Click OK to select the configuration YAML file to use.', 'SEFIS Video Utility', 'OK', 'Information')"

echo Attempting to open file explorer...

:: 2. Open the File Explorer Dialog
for /f "usebackq delims=" %%I in (`powershell -noprofile -command "Add-Type -AssemblyName System.Windows.Forms; $f = New-Object System.Windows.Forms.OpenFileDialog; $f.Filter = 'YAML Files (*.yml)|*.yml'; $f.InitialDirectory = '%CD%'; if($f.ShowDialog() -eq 'OK') { $f.FileName }"`) do set "config_file=%%I"

:: 3. Final check
if "%config_file%"=="" (
    echo ERROR: No configuration file provided. 
    pause
    exit /b
)

echo.
echo Processing: "%config_file%"
echo -----------------------------------------------------------

:: Execute using the hidden .venv folder
.venv\Scripts\python clip-and-stitch.py "%config_file%"

echo.
echo -----------------------------------------------------------
echo Processing finished.
pause