@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo 安装「政策雷达」定时巡检：每 30 分钟跑一次，只在后台抓取，不弹窗。
echo.
schtasks /create /tn "政策雷达-政策巡检" /tr "\"%~dp0run_silent.vbs\"" /sc minute /mo 30 /f
if %errorlevel%==0 (
  echo.
  echo 已安装。之后每 30 分钟自动抓取，打开 out\index.html 就是最新的。
  echo 删除定时任务： schtasks /delete /tn "政策雷达-政策巡检" /f
) else (
  echo.
  echo 安装失败：请右键「以管理员身份运行」再试一次。
)
echo.
pause
