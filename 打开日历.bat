@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================
echo   政策雷达 · 正在抓取最新政策
echo ================================
python radar.py sync
echo.
echo 日历已更新，正在打开...
start "" "%~dp0out\index.html"
