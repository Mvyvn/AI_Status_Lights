@echo off
chcp 65001 > nul
cd /d "%~dp0"
title AI Status Lights 上位机

python --version > nul 2>&1
if errorlevel 1 (
    echo [x] 没有找到 python, 请先安装 Python 3 并勾选 "Add Python to PATH"
    pause
    exit /b 1
)

python -c "import serial" > nul 2>&1
if errorlevel 1 (
    echo [i] 缺少 pyserial, 正在安装...
    python -m pip install pyserial
    if errorlevel 1 (
        echo [x] pyserial 安装失败, 请手动执行: python -m pip install pyserial
        pause
        exit /b 1
    )
)

echo [i] 启动 AI Status Lights 上位机...
python ai_status_lights_gui.py %*
if errorlevel 1 (
    echo.
    echo [x] 程序退出, 上面有错误信息
    pause
)
