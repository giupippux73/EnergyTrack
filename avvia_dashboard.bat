@echo off
title Dashboard Energia - LifeOS
cd /d "%~dp0"
echo.
echo  ====================================================
echo   ?  Dashboard Energia ? LifeOS
echo   Avvio in corso...
echo  ====================================================
echo.
python app.py
if %ERRORLEVEL% NEQ 0 (
    echo.
    echo  ERRORE: impossibile avviare il server.
    echo  Assicurati che Python sia installato e le librerie siano presenti.
    echo  Premi un tasto per chiudere.
    pause >nul
)
