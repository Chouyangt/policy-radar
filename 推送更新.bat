@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo ================================================
echo   政策雷达 · 推送本地改动到 GitHub
echo ================================================
echo.

echo [1/3] 同步远端（Actions 会自动提交数据库，先拉再推）
git pull --rebase origin main
if errorlevel 1 (
  echo.
  echo   × 拉取失败。如果提示冲突，把上面的信息发我。
  pause & exit /b 1
)

echo.
echo [2/3] 提交本地改动
git add -A
git commit -m "update: %date% %time%"
if errorlevel 1 (
  echo   （没有新改动，跳过提交）
)

echo.
echo [3/3] 推送
git push origin main
if errorlevel 1 (
  echo.
  echo   × 推送失败。把上面的信息发我。
  pause & exit /b 1
)

echo.
echo ================================================
echo   已推送。GitHub 大约 1 分钟后会自动跑一轮：
echo   抓取 -^> 分析 -^> 部署到 Pages
echo.
echo   看进度：https://github.com/Chouyangt/policy-radar/actions
echo   看结果：https://chouyangt.github.io/policy-radar/
echo ================================================
echo.
pause
