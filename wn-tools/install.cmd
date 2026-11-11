@echo off
setlocal
set "HERE=%~dp0"
set "TARGET=%LOCALAPPDATA%\wn-tools"
if not exist "%HERE%bin\wn-tools.exe" (
	echo %HERE%bin\wn-tools.exe was not found. Run build.cmd first.
	exit /b 1
)
taskkill /im wn-tools.exe /f >nul 2>&1
if not exist "%TARGET%" mkdir "%TARGET%"
copy /y "%HERE%bin\wn-tools.exe" "%TARGET%\wn-tools.exe" >nul || exit /b 1
if exist "%HERE%wn-tools.ini" if not exist "%TARGET%\wn-tools.ini" copy /y "%HERE%wn-tools.ini" "%TARGET%\wn-tools.ini" >nul
reg add "HKCU\Software\Microsoft\Windows\CurrentVersion\Run" /v wn-tools /t REG_SZ /d "\"%TARGET%\wn-tools.exe\"" /f >nul || exit /b 1
start "" "%TARGET%\wn-tools.exe"
echo Installed %TARGET%\wn-tools.exe. It starts when you sign in.
