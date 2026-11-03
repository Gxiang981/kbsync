@echo off
rem ---------------------------------------------------------------------------
rem kbsync Windows 启动器（pythonw = 无黑窗）
rem 把下面两个路径改成你自己的，然后双击运行 / 放进「启动」文件夹 / 任务计划程序。
rem ---------------------------------------------------------------------------
set "PYTHONW=%LOCALAPPDATA%\Programs\Python\Python312\pythonw.exe"
set "HERE=%~dp0"

if not exist "%PYTHONW%" (
  echo 找不到 pythonw: %PYTHONW%
  echo 请修改本文件里的 PYTHONW 路径。
  pause
  exit /b 1
)
if not exist "%HERE%config.json" (
  echo 找不到 %HERE%config.json
  echo 请先复制 config.example.json 成 config.json 并修改。
  pause
  exit /b 1
)

start "" "%PYTHONW%" "%HERE%kbsync.py" --config "%HERE%config.json"
