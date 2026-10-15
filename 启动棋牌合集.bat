@echo off
chcp 65001 >nul
title 棋牌合集统一测试版
cd /d %~dp0

where py >nul 2>&1
if %errorlevel%==0 (
  py 启动棋牌合集.py
) else (
  python 启动棋牌合集.py
)

echo.
echo 棋牌合集已关闭。
pause
