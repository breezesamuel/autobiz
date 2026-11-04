@echo off
rem ============================================================
rem  AutoBiz 开机自启脚本（已安装到 Windows 启动文件夹）
rem  作用：登录后自动拉起 24/7 守护循环 + 控制台，重启不中断
rem  卸载：删除启动文件夹中的 AutoBiz-autostart.cmd 即可
rem ============================================================
set "PY=C:\Users\46012\AppData\Roaming\TRAE SOLO CN\ModularData\ai-agent\vm\tools\python"
set "APP=f:\2026\diqi"
start "" /B "%PY%\pythonw.exe" "%APP%\run.py" daemon --interval 3600
start "" /B "%PY%\pythonw.exe" "%APP%\run.py" dashboard
