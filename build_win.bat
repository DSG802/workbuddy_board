@echo off
REM WorkBuddyBoard Windows build -- single-file exe (--onefile).
REM Needs Python 3.10+; Win10/11 already ships the Edge WebView2 runtime.
REM NOTE: keep this file pure ASCII and CRLF. cmd.exe on a GBK console mangles
REM       UTF-8 Chinese in .bat, which used to break the build with errors like
REM       "'ho' is not recognized".
REM
REM Usage:  build_win.bat [version-label]
REM           build_win.bat v8-20261114
REM         -> dist\WorkBuddyBoard.exe  and  WorkBuddyBoard-win-x64-v8-20261114.exe
setlocal
cd /d "%~dp0"

set "VER=%~1"
if "%VER%"=="" set "VER=dev"

echo [1/5] preparing venv
if not exist .venv (
  python -m venv .venv || goto :fail
  .venv\Scripts\python -m pip install -q --upgrade pip
  .venv\Scripts\python -m pip install -q pywebview pyinstaller || goto :fail
) else (
  echo       venv exists
)

REM ---- build tag --------------------------------------------------------------
REM BUILD_TAG is read by app\checkin.py at RUNTIME, not at build time. Setting it
REM here only affects this shell; whoever double-clicks the exe reads their own
REM environment and falls back to the constant. In --onefile the app\ tree is
REM baked into the exe, so it can no longer be patched after packaging -- the
REM stamp step therefore rewrites a staging copy BEFORE PyInstaller runs and
REM leaves the tracked source alone (android keeps a byte-identical copy of
REM app\checkin.py, so touching it in place would break that invariant).
REM The tag is decided by whoever releases, not by each build machine: an inbound
REM BUILD_TAG wins; only fall back to the local clock when nobody set one.
echo [2/5] deciding build tag
if not defined BUILD_TAG (
  for /f %%i in ('powershell -NoProfile -Command "Get-Date -Format yyyy-MM-dd.HHmm"') do set "BUILD_TAG=%%i"
  echo       no inbound BUILD_TAG, using local clock
) else (
  echo       using inbound BUILD_TAG
)
if not defined BUILD_TAG set "BUILD_TAG=unknown"
echo       BUILD_TAG=%BUILD_TAG%

echo [3/5] staging app tree
if exist ".stage" rmdir /s /q ".stage"
xcopy "app" ".stage\app" /e /i /q /y >nul || goto :fail
if exist ".stage\app\__pycache__" rmdir /s /q ".stage\app\__pycache__"
powershell -NoProfile -ExecutionPolicy Bypass -File "tools\stamp_build_win.ps1" -AppDir ".stage\app" -Tag "%BUILD_TAG%" || goto :fail

echo [4/5] packaging single-file exe
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist

.venv\Scripts\pyinstaller --noconfirm --clean --windowed --onefile ^
  --name WorkBuddyBoard ^
  --icon assets\icon.ico ^
  --hidden-import sqlite3 ^
  --hidden-import _sqlite3 ^
  --add-data ".stage\app;app" ^
  --add-data "pets;pets" ^
  desktop.py || goto :fail

if not exist "dist\WorkBuddyBoard.exe" (
  echo       ERROR: dist\WorkBuddyBoard.exe not produced
  goto :fail
)
set "EXE=WorkBuddyBoard-win-x64-%VER%.exe"
copy /y "dist\WorkBuddyBoard.exe" "%EXE%" >nul || goto :fail

echo [5/5] checksum
REM certutil prints the digest alone on the middle line, between a localized
REM header and a localized trailer. The previous version piped through
REM findstr /v ":" (which already drops the header) and then ALSO asked /f for
REM skip=1, so the two filters together threw the digest away and HASH stayed
REM empty. Match the digest by shape instead -- hex only, no colons -- which is
REM independent of the console language and of the trailer wording.
set "HASH="
for /f "delims=" %%h in ('certutil -hashfile "%EXE%" SHA256 ^| findstr /r "^[0-9a-f][0-9a-f]*$"') do set "HASH=%%h"
if defined HASH set "HASH=%HASH: =%"
if not defined HASH (
  echo       ERROR: could not compute sha256
  goto :fail
)
> SHA256SUMS.txt echo %HASH%  %EXE%

echo.
echo done.
echo   exe : %EXE%
echo   tag : %BUILD_TAG%
echo   data: %%APPDATA%%\WorkBuddyBoard\
endlocal
exit /b 0

:fail
echo.
echo BUILD FAILED - see the errors above.
endlocal
exit /b 1
