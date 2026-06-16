@echo off
title LegacyMigrator - Servidor Local
color 0A
echo.
echo  ==========================================
echo    LegacyMigrator - Iniciando servidor...
echo  ==========================================
echo.
echo  Abriendo navegador en http://localhost:8080
echo  Para cerrar este servidor presiona Ctrl+C
echo.
start "" http://localhost:8080
python servidor.py
pause
