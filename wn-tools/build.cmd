@echo off
setlocal
set "HERE=%~dp0"
set "CSC=%WINDIR%\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
if not exist "%CSC%" set "CSC=%WINDIR%\Microsoft.NET\Framework\v4.0.30319\csc.exe"
if not exist "%CSC%" (
	echo csc.exe from .NET Framework 4.8 was not found.
	exit /b 1
)
if not exist "%HERE%bin" mkdir "%HERE%bin"
"%CSC%" /nologo /target:winexe /optimize+ /out:"%HERE%bin\wn-tools.exe" /reference:System.dll /reference:System.Drawing.dll /reference:System.Windows.Forms.dll /reference:System.Net.Http.dll /reference:System.Web.Extensions.dll "%HERE%src\*.cs"
if errorlevel 1 exit /b 1
echo Built %HERE%bin\wn-tools.exe
