@echo off
cd /d "%~dp0"
rem Abre o painel ao vivo. Se o vigia não estiver rodando, liga ele antes.
where pyw >nul 2>nul
if %errorlevel%==0 (start "" pyw -3 vigia.py --abrir) else (start "" pythonw vigia.py --abrir)
