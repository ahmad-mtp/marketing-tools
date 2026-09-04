@echo off
REM Start Chrome on the HOST with a debugging port the container can attach to.
REM Usage: launch-chrome.bat [client-name]
REM
REM SECURITY: --remote-debugging-address=0.0.0.0 exposes full control of this
REM browser to anything that can reach port 9222. Trusted networks only.

set CLIENT=%1
if "%CLIENT%"=="" set CLIENT=default
set PORT=9222
set PROFILE=%USERPROFILE%\.linkedin-harvester\profiles\%CLIENT%
set CHROME=C:\Program Files\Google\Chrome\Application\chrome.exe
if not exist "%CHROME%" set CHROME=C:\Program Files (x86)\Google\Chrome\Application\chrome.exe

if not exist "%PROFILE%" mkdir "%PROFILE%"
echo Client:  %CLIENT%
echo Profile: %PROFILE%
echo CDP:     http://127.0.0.1:%PORT%

start "" "%CHROME%" --remote-debugging-port=%PORT% --remote-debugging-address=0.0.0.0 --remote-allow-origins=* --user-data-dir="%PROFILE%" --no-first-run --no-default-browser-check https://www.linkedin.com/
