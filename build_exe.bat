@echo off
setlocal
rem build_exe.bat - build CoD4toSOF2.exe from this folder (Windows).
rem Needs Python 3.8+ with the py launcher. Everything else is installed here.
cd /d "%~dp0"

set "PY=py -3"
%PY% --version >nul 2>&1 || set "PY=python"
%PY% --version || ( echo Python not found. Install it from python.org and tick "Add to PATH". & pause & exit /b 1 )

echo.
echo === installing build requirements ===
%PY% -m pip install --upgrade pip                      || goto fail
%PY% -m pip install pyinstaller pillow numpy customtkinter || goto fail

echo.
echo === checking (builds the whole GUI headless first) ===
%PY% gui_smoke_test.py                                 || goto fail

echo.
echo === third-party licences ===
rem Written from the packages actually installed above, so the texts match
rem what PyInstaller is about to bundle. Done before the build so the .spec
rem can put the file inside the exe as well as beside it.
%PY% collect_licenses.py THIRD-PARTY-LICENSES.txt      || goto licfail
goto licok
:licfail
echo.
echo Could not collect every third-party licence - see the warning above.
echo The exe bundles that code, so it should not ship without the text.
pause
exit /b 1
:licok

echo.
echo === building ===
rem NOT "rmdir /s /q dist": the GUI's default output folder is next to the
rem exe, so on a dev layout that is dist\output - and deleting it threw away
rem the converted map AND cod4tosof2_budget.json, which is where the prop
rem budget and the vertex-lit-terrain decision live. Losing that silently put
rem mp_derail back over MAX_SHADERS on the next compile. Only the exe goes.
rmdir /s /q build 2>nul
del /q "dist\CoD4toSOF2.exe" 2>nul
rem If the exe survived that delete it is locked - the GUI is still open, or
rem Explorer is holding it - and the build would leave yesterday's exe sitting
rem there looking new. Say so instead.
if exist "dist\CoD4toSOF2.exe" (
    echo.
    echo Cannot replace dist\CoD4toSOF2.exe - it is in use.
    echo Close CoD4toSOF2.exe ^(and any Explorer window showing dist^) and run this again.
    pause
    exit /b 1
)
%PY% -m PyInstaller --noconfirm CoD4toSOF2.spec        || goto fail
if not exist "dist\CoD4toSOF2.exe" goto fail

rem compile_cod4_map.bat, paths.cmd.example and README.md are bundled inside
rem the exe by the .spec (datas) and read back with res_dir() - nothing to copy.

rem Beside the exe as well as inside it: someone who downloads the exe on its
rem own should be able to read the licences without running anything.
copy /y "LICENSE" "dist\LICENSE" >nul 2>&1
copy /y "THIRD-PARTY-LICENSES.txt" "dist\THIRD-PARTY-LICENSES.txt" >nul 2>&1

echo.
echo === done ===
for %%F in ("dist\CoD4toSOF2.exe") do echo   %%~fF   %%~tF
%PY% -c "import cod4tosof2_run as r; print('   build ' + r.VERSION)"
echo.
echo That full path is the exe to run. The first line of every Convert and
echo Compile log repeats the build above - if a log shows an older one, the
echo exe being run is not this one.
pause
exit /b 0

:fail
echo.
echo BUILD FAILED
pause
exit /b 1
