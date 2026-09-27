@echo off
chcp 65001 >nul 2>&1
set PYTHONIOENCODING=utf-8
title Script2

cd /d "%~dp0"

set PY=C:\Users\alecl\AppData\Local\Programs\Python\Python313\python.exe
set CF=C:\Users\alecl\AppData\Local\Temp\opencode\cloudflared.exe
set CFLOG=%TEMP%\opencode\cf_bat.log
if exist "%CFLOG%" del "%CFLOG%" >nul 2>&1

echo ==============================================
echo   AVVIO Script2
echo ==============================================
echo.

echo [1/4] Chiusura preventiva di eventuali processi precedenti...
taskkill /f /im cloudflared.exe >nul 2>&1
for /f "tokens=5" %%a in ('netstat -aon ^| findstr ":5000" ^| findstr "LISTENING"') do taskkill /f /pid %%a >nul 2>&1
timeout /t 1 /nobreak >nul

echo [2/4] Avvio il server Flask su 127.0.0.1:5000...
if not exist "%PY%" (
    echo [ERRORE] Python non trovato: %PY%
    pause
    exit /b 1
)
start "Script2 - Flask" /min "%PY%" app.py
timeout /t 2 /nobreak >nul

echo [3/4] Avvio il tunnel Cloudflare (trycloudflare.com)...
if not exist "%CF%" (
    echo [ERRORE] cloudflared.exe non trovato: %CF%
    pause
    exit /b 1
)
start "Script2 - Tunnel" /min "%CF%" tunnel --edge-ip-version 4 --protocol http2 --url http://127.0.0.1:5000 --no-autoupdate --logfile "%CFLOG%" --loglevel info

echo [4/4] Attendo generazione del link Cloudflare e aggiorno GitHub...
set "PUBLIC="
for /L %%i in (1,1,25) do (
    if not defined PUBLIC (
        timeout /t 1 /nobreak >nul
        if exist "%CFLOG%" (
            for /f "tokens=*" %%L in ('findstr /C:"trycloudflare.com" "%CFLOG%"') do (
                for /f "tokens=3" %%U in ("%%L") do set "PUBLIC=%%U"
            )
        )
    )
)
if defined PUBLIC (
    set PUBLIC=%PUBLIC:^|=%
    echo   Link pubblico: %PUBLIC%
    echo   Aggiorno https://luishighnest.github.io/script2/ ...
    "%PY%" update_redirect.py "%PUBLIC%"
    echo.
    echo ==============================================
    echo   SITO ONLINE
    echo   Da condividere (link fisso): https://luishighnest.github.io/script2/
    echo   Link pubblico attuale:        %PUBLIC%
    echo   Link locale:                  http://localhost:5000
    echo ==============================================
) else (
    echo   Il link non e' ancora pronto.
    echo   Guarda tra poco nella finestra "Script2 - Tunnel", poi lancia:
    echo   "%PY%" update_redirect.py IL-LINK
)
echo.

start http://localhost:5000
pause