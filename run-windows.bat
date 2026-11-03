@echo off
rem kbsync launcher (no console window).
rem For auto-start, copy this file into:
rem   %APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\
rem It must run in the interactive session; otherwise cursor sampling fails
rem and the agent cannot tell which machine you are actually using.
start "" "%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe" "%~dp0kbsync.py" --config "%~dp0config.json"
