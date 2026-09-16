@echo off
setlocal enabledelayedexpansion
title Dola Render Gateway
color 0b

echo =====================================================================
echo                DOLA RENDER GATEWAY - KHOI DONG HE THONG
echo =====================================================================
echo.

cd /d "%~dp0"

:: 1. Kiem tra Python
python --version >nul 2>&1
if %errorlevel% neq 0 (
    echo [LOI] Khong tim thay Python tren he thong!
    echo Vui long cai dat Python 3.11 hoac 3.12 tu python.org va tich chon "Add Python to PATH".
    echo.
    pause
    exit /b 1
)

for /f "tokens=2 delims= " %%v in ('python --version 2^>^&1') do set PY_VER=%%v
echo [1/5] Da tim thay Python: !PY_VER!

:: 2. Kiem tra va tao moi virtual environment (.venv)
if not exist ".venv\Scripts\activate.bat" (
    echo [2/5] Chua co moi truong ao .venv. Dang khoi tao...
    python -m venv .venv
    if !errorlevel! neq 0 (
        echo [LOI] Khong the tao virtual environment!
        pause
        exit /b 1
    )
    echo    -^> Khoi tao .venv thanh cong!
) else (
    echo [2/5] Da tim thay moi truong ao .venv.
)

:: Kich hoat virtual environment
call .venv\Scripts\activate.bat

:: 3. Kiem tra va cai dat dependencies
echo [3/5] Kiem tra thu vien phu thuoc (requirements)...
python -c "import fastapi, uvicorn, patchright, PIL, cv2" >nul 2>&1
if !errorlevel! neq 0 (
    echo    -^> Dang cai dat dependencies tu requirements.txt...
    call python -m pip install -r requirements.txt
    if !errorlevel! neq 0 (
        echo [LOI] Cai dat dependencies that bai!
        pause
        exit /b 1
    )
    echo    -^> Dang cai dat Chromium cho patchright...
    call patchright install chromium
) else (
    echo    -^> Cac thu vien da duoc cai dat day du.
)

:: 4. Kiem tra file cau hinh .env.local
echo [4/5] Kiem tra file cau hinh moi truong...
if not exist ".env.local" (
    echo    -^> Chua co file .env.local. Dang tao file mau...
    > .env.local echo # Cau hinh Dola Render Gateway
    >> .env.local echo DOLA_HOST=0.0.0.0
    >> .env.local echo DOLA_PORT=8000
    >> .env.local echo.
    >> .env.local echo # Proxy HTTP neu co (de trong neu dung VPN hoac mang truc tiep)
    >> .env.local echo DOLA_PROXY=
    >> .env.local echo.
    >> .env.local echo # So luong tien trinh render dong thoi
    >> .env.local echo DOLA_MAX_CONCURRENCY=3
    >> .env.local echo.
    >> .env.local echo # API Key xac thuc client
    >> .env.local echo DOLA_API_KEYS=
    >> .env.local echo.
    >> .env.local echo # Mat khau trang Admin Web Dashboard
    >> .env.local echo DOLA_ADMIN_KEY=
    echo    -^> Da tao file .env.local!
) else (
    echo    -^> Tim thay file .env.local.
)

:: Tao cac thu muc can thiet neu chua co
if not exist "downloads" mkdir downloads
if not exist "accounts" mkdir accounts

:: 5. Khoi chay FastAPI server
echo.
echo [5/5] Dang khoi chay Dola Render Gateway Server...
echo ---------------------------------------------------------------------
echo  * Admin Dashboard:  http://127.0.0.1:8000 (hoac http://127.0.0.1:8000/web)
echo  * Swagger API Docs: http://127.0.0.1:8000/docs
echo ---------------------------------------------------------------------
echo.

python -c "import uvicorn, config; uvicorn.run('server:app', host=config.HOST, port=config.PORT)"

echo.
echo [THONG BAO] Server da ket thuc hoac bi dung!
pause
