@echo off
chcp 65001 >nul
cd /d "%~dp0"
where py >nul 2>nul
if %errorlevel%==0 (py -3 iniciar.py --so-painel --sem-atualizar) else (python iniciar.py --so-painel --sem-atualizar)
echo.
pause
