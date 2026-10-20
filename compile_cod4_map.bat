@echo off
setlocal
rem set COD4TOSOF2_DEBUG=1 before running this to echo every line it executes
if defined COD4TOSOF2_DEBUG echo on
rem compile_cod4_map.bat <mapname> [fast]
rem   e.g.  compile_cod4_map.bat mp_backlot_geo fast
rem Installs a converted CoD4 map (CoD4toSOF2 output) into SOF2, then compiles it.
rem "fast" = test build with -vis -fast. Without a map name it asks for one.
rem
rem Paths. The GUI's Compile button passes these in the environment, so there
rem is nothing to edit when you use it. Running this .bat on its own, either
rem put them in a file called paths.cmd next to it (see paths.cmd.example) or
rem edit the three lines below.
rem   Q3MAP2   full path to q3map2.exe (NetRadiant / GtkRadiant)
rem   GAMEROOT folder that CONTAINS base\  (not base\ itself)
rem   OUTDIR   the converter's output folder, the one holding <mapname>\
if defined COD4TOSOF2_HOME if exist "%COD4TOSOF2_HOME%paths.cmd" call "%COD4TOSOF2_HOME%paths.cmd"
if exist "%~dp0paths.cmd" call "%~dp0paths.cmd"
if not defined Q3MAP2   set "Q3MAP2=C:\Games\NetRadiant\q3map2.exe"
if not defined GAMEROOT set "GAMEROOT=C:\Games\Soldier of Fortune II - Double Helix"
if not defined OUTDIR   set "OUTDIR=%~dp0output"

rem Helper scripts. Both ways of running end in the same shape:
rem     %PYCMD% <stepname> <args...>
rem   built exe : "...\CoD4toSOF2.exe" --step <stepname>
rem   from source: python "...\cod4tosof2_step.py" <stepname>
rem SCRIPTDIR always ends with a backslash (%~dp0 does; the GUI sends one).
set "SCRIPTDIR=%COD4TOSOF2_DIR%"
if not defined SCRIPTDIR set "SCRIPTDIR=%~dp0"
if defined COD4TOSOF2_EXE goto pyexe
set PYCMD=python "%SCRIPTDIR%cod4tosof2_step.py"
set "PYSRC=1"
goto pydone
:pyexe
set PYCMD="%COD4TOSOF2_EXE%" --step
:pydone
set "NAME=%~1"
set "OPT=%~2"
if not "%NAME%"=="" goto haveName
rem double-clicked: ask, and accept "mp_backlot_geo fast" at the prompt too
set "ANSWER="
set /p "ANSWER=Map name, optionally followed by fast (e.g. mp_backlot_geo fast): "
for /f "tokens=1,2" %%a in ("%ANSWER%") do (
    set "NAME=%%a"
    set "OPT=%%b"
)
:haveName
if "%NAME%"=="" goto fail
set "SHADER=cod4_%NAME%"
set "BASE=%GAMEROOT%\base"
set "MAP=%BASE%\maps\%NAME%"
set "VISFAST="
if /i "%OPT%"=="fast" set "VISFAST=-fast"
if defined VISFAST echo TEST BUILD: -vis -fast

if exist "%Q3MAP2%" goto haveq3map2
echo q3map2 not found: %Q3MAP2%
goto fail
:haveq3map2
if exist "%BASE%\*" goto havebase
echo SOF2 base folder not found: %BASE%
goto fail
:havebase

rem --- find the converted output (folder holding maps\ models\ shaders\ textures\)
set "SRC="
for %%D in ("%OUTDIR%\%NAME%" "%~dp0." "%~dp0..") do (
    if not defined SRC if exist "%%~fD\maps\%NAME%.map" if exist "%%~fD\shaders\%SHADER%.shader" set "SRC=%%~fD"
)
if not defined SRC (
    echo Converted output not found. Looked for maps\%NAME%.map + shaders\%SHADER%.shader in:
    echo   %OUTDIR%\%NAME%
    echo   %~dp0
    echo   %~dp0..
    echo Run Convert in the GUI first, or set OUTDIR - see the top of this file.
    goto fail
)
rem Keep in step with VERSION in cod4tosof2_run.py. This file is bundled
rem inside the exe and read back from there, so the two lines in a Compile log
rem always match - unless the exe being run is not the one just built, which
rem is what made "-bounce" keep showing up in a log after it was removed.
echo compile_cod4_map.bat 2026.10.20.6
echo Converted output: %SRC%

rem --- the helpers must be runnable (exe step, or the .py files next to us)
if not defined PYSRC goto havehelpers
if exist "%SCRIPTDIR%cod4tosof2_step.py" goto havestep
echo cod4tosof2_step.py not found in %SCRIPTDIR%
goto fail
:havestep
if exist "%SCRIPTDIR%bsp_fix_leafbounds.py" goto havehelpers
echo bsp_fix_leafbounds.py not found in %SCRIPTDIR%
goto fail
:havehelpers

echo Installing into %BASE% ...
xcopy "%SRC%\maps\%NAME%.map" "%BASE%\maps\" /I /Y /Q >nul || goto fail
xcopy "%SRC%\models\cod4" "%BASE%\models\cod4\" /E /I /Y /Q >nul || goto fail
xcopy "%SRC%\textures\cod4" "%BASE%\textures\cod4\" /E /I /Y /Q >nul || goto fail
xcopy "%SRC%\shaders\%SHADER%.shader" "%BASE%\shaders\" /I /Y /Q >nul || goto fail
rem q3map2 and SOF2 only load .shader files named in shaderlist.txt
findstr /X /I /C:"%SHADER%" "%BASE%\shaders\shaderlist.txt" >nul 2>&1 || (
    echo.>>"%BASE%\shaders\shaderlist.txt"
    echo %SHADER%>>"%BASE%\shaders\shaderlist.txt"
    echo   added %SHADER% to shaderlist.txt
)

rem Build, then check the lightmap/shader pairs. Every shader x lightmap page
rem is its own renderer shader and SOF2 has 2048 in all, shared with the props'
rem shaders and everything the game loads. The count comes from the finished
rem .bsp (bsp_budget.py pairs), NOT from q3map2's "unique lightmap/shader
rem combinations" line, which ran about half the real figure on every map
rem measured - mp_strike 628 vs 1135, mp_derail 1030 vs 2057. Measured:
rem mp_strike loads at 1135; mp_derail hit MAX_SHADERS in the console at 2057.
rem Over MAXPAIRS the map is built again with coarser lightmaps (worldspawn
rem _lightmapscale 1.5, 2, 3, 4, then 6).
rem (Bigger external lightmap pages - update28-30 - are no longer used: they
rem only reached a few shaders, came out darker and spammed "mixed mipmap".)
set "MAXPAIRS=1400"
set "LIGHTLOG=%MAP%_light.log"
set "SETKEY=1"
set "TEEPY=1"
set "PAIRCHK=1"
if not defined PYSRC goto haveopt
if not exist "%SCRIPTDIR%map_set_key.py" set "SETKEY="
if not exist "%SCRIPTDIR%run_tee.py" set "TEEPY="
if not exist "%SCRIPTDIR%bsp_budget.py" set "PAIRCHK="
:haveopt
set "TJ="
rem Start the ladder where the last compile of this map ended, not at 1. Every
rem rung costs a full rebuild and vis is the expensive part - mp_strike spent
rem 4 x 570 seconds of vis walking 1, 1.5, 2, 3 to arrive at 3, and all four
rem vis passes computed exactly the same thing, because _lightmapscale changes
rem nothing vis looks at. bsp_budget remembers the rung and gives one step
rem finer back when the map comes in well under the target.
set "LMSCALE=1"
if not defined PAIRCHK goto lmready
%PYCMD% bsp_budget lmscale "%OUTDIR%" "%NAME%" > "%MAP%_lm.txt"
if not exist "%MAP%_lm.txt" goto lmready
set /p LMSCALE=<"%MAP%_lm.txt"
del /q "%MAP%_lm.txt" >nul 2>&1
:lmready
if not defined LMSCALE set "LMSCALE=1"
set "LMOK="
for %%s in (1 1.5 2 3 4) do if "%LMSCALE%"=="%%s" set "LMOK=1"
if not defined LMOK set "LMSCALE=1"
if not "%LMSCALE%"=="1" echo starting at _lightmapscale %LMSCALE% - what the last compile of this map needed
rem a fresh map from Convert has no _lightmapscale; this also resets one left
rem by an earlier compile of the same .map
if defined SETKEY %PYCMD% map_set_key "%MAP%.map" _lightmapscale %LMSCALE% || goto fail
call :build || goto fail
:pairloop
if %PAIRS% LEQ %MAXPAIRS% goto lightdone
if not defined SETKEY goto pairsgiveup
if "%LMSCALE%"=="1" ( set "LMSCALE=1.5" & goto rescale )
if "%LMSCALE%"=="1.5" ( set "LMSCALE=2" & goto rescale )
if "%LMSCALE%"=="2" ( set "LMSCALE=3" & goto rescale )
rem Measured on mp_derail: with the terrain still lightmapped, _lightmapscale
rem 4 moved its luxels only from 5,292,032 to 5,177,344 and its pages from 323
rem to 316. worldspawn _lightmapscale does not reach misc_model surfaces, and
rem the baked terrain IS a misc_model - so the ladder is close to useless
rem while the terrain holds the lightmaps. Once it went vertex-lit the same
rem ladder worked properly: 1,818,624 luxels and 111 pages at scale 1, down to
rem 1,064,960 and 65 at scale 1.5, and 1317 pairs.
rem The ladder stops at 4 (64 units per luxel). Past that, coarsening the
rem whole map is the wrong trade: the baked terrain is the biggest consumer of
rem the shader pool, and vertex-lighting it keeps ~61-unit detail there (its
rem control points average 61 units apart) against the 96 that _lightmapscale
rem 6 would give everything. So the next Convert switches the terrain instead
rem - bsp_budget records the pair count and cod4mat2shader acts on it.
if "%LMSCALE%"=="3" ( set "LMSCALE=4" & goto rescale )
:pairsgiveup
echo WARNING: still %PAIRS% shader/lightmap pairs against MAX_SHADERS 2048 -
echo          expect "MAX_SHADERS" in the console and surfaces with no shader.
echo          The count is recorded, so Convert this map again and the baked
echo          terrain comes out vertex-lit, which is worth several hundred
echo          pairs. Then Compile once more.
goto lightdone
:rescale
echo.
echo %PAIRS% lightmap/shader pairs is above %MAXPAIRS%: building again with _lightmapscale %LMSCALE% (coarser lightmaps)...
%PYCMD% map_set_key "%MAP%.map" _lightmapscale %LMSCALE% || goto fail
call :build || goto fail
goto pairloop
:lightdone
for %%A in ("%MAP%.bsp") do set "BSPBYTES=%%~zA"
set /a BSPMB=BSPBYTES/1048576
echo Done: %MAP%.bsp (%BSPMB% MB)
rem how many model_statics a player can have in view at once (snapshot limit 256)
set "PVSCHK=1"
if defined PYSRC if not exist "%SCRIPTDIR%bsp_static_pvs.py" set "PVSCHK="
set "BUDGETCHK=1"
if defined PYSRC if not exist "%SCRIPTDIR%bsp_budget.py" set "BUDGETCHK="
if not exist "%SRC%\%NAME%_cod4_manifest.json" set "BUDGETCHK="
if defined PVSCHK %PYCMD% bsp_static_pvs "%MAP%.bsp"
rem SOF2 loads the .bsp in one block: 57 MB (mp_shipment) loads, 68 MB (mp_backlot) did not
rem Pair the finished size with the baked prop count, so the next Convert of
rem this map aims its budget at the real file size instead of a guess.
if defined BUDGETCHK %PYCMD% bsp_budget record "%OUTDIR%" "%NAME%" "%MAP%.bsp" "%SRC%\%NAME%_cod4_manifest.json" %PAIRS% %LMSCALE%
rem 64 MB is what loads at the stock com_hunkMegs 512/256. A bigger hunk is
rem reserved as one block up front and leaves less contiguous room for the
rem .bsp, so the same map can die in Z_Malloc at com_hunkMegs 1024.
if not defined COD4TOSOF2_BSPMAX set "COD4TOSOF2_BSPMAX=64"
if %BSPMB% GEQ %COD4TOSOF2_BSPMAX% (
    echo WARNING: %BSPMB% MB is at or over %COD4TOSOF2_BSPMAX% MB and may fail with Z_Malloc.
    echo          SOF2 is 32-bit and reads the whole .bsp in ONE allocation, so what
    echo          fits depends on com_hunkMegs - 512 or 256 loads maps that fail at 1024.
    echo          The next Convert of this map will aim lower on its own.
)
if not defined COD4TOSOF2_NOPAUSE pause
exit /b 0
:build
rem leftovers of update28-30's lightmap pages: out of shaderlist.txt, deleted
call :unregister_pages
rem -bsp. Detailed maps (mp_backlot) can have more than 65536 t-junction
rem edge lines (q3map2 "MAX_EDGE_LINES"); then build again with -notjunc.
"%Q3MAP2%" -game sof2 -fs_basepath "%GAMEROOT%" -meta -rename %TJ% -v "%MAP%.map" && goto bspok
if defined TJ exit /b 1
echo.
echo -bsp failed. Building again without t-junction fixing (-notjunc)...
set "TJ=-notjunc"
"%Q3MAP2%" -game sof2 -fs_basepath "%GAMEROOT%" -meta -rename %TJ% -v "%MAP%.map" || exit /b 1
:bspok
rem Big maps: T-junction fixing adds vertices (mp_strike: 75,906, about 6 MB).
rem SOF2 loads the .bsp in one block, so above 45 MB after -bsp build again
rem without it: an occasional pixel sparkle along a seam beats a map that won't load.
for %%A in ("%MAP%.bsp") do set "BSPBYTES=%%~zA"
set /a BSPMB=BSPBYTES/1048576
if defined TJ goto tjdone
if %BSPMB% LSS 45 goto tjdone
echo.
echo -bsp output is %BSPMB% MB: building again without t-junction fixing (-notjunc) to save space...
set "TJ=-notjunc"
"%Q3MAP2%" -game sof2 -fs_basepath "%GAMEROOT%" -meta -rename %TJ% -v "%MAP%.map" || exit /b 1
:tjdone
%PYCMD% bsp_fix_leafbounds "%MAP%.bsp" || exit /b 1
"%Q3MAP2%" -game sof2 -fs_basepath "%GAMEROOT%" -vis %VISFAST% -saveprt -v "%MAP%.bsp" && goto visdone
rem -vis fails with MAX_MAP_VISIBILITY when the map has more than 16384 vis clusters;
rem hint brushes are the usual cause, so build again without them.
echo.
echo -vis failed. Rebuilding without the hint brushes (-nohint) and trying again...
"%Q3MAP2%" -game sof2 -fs_basepath "%GAMEROOT%" -meta -rename -nohint %TJ% -v "%MAP%.map" || exit /b 1
%PYCMD% bsp_fix_leafbounds "%MAP%.bsp" || exit /b 1
"%Q3MAP2%" -game sof2 -fs_basepath "%GAMEROOT%" -vis %VISFAST% -saveprt -v "%MAP%.bsp" || exit /b 1
:visdone
call :light || exit /b 1
exit /b 0

:unregister_pages
if exist "%BASE%\shaders\q3map2_%NAME%.shader" del /q "%BASE%\shaders\q3map2_%NAME%.shader"
if exist "%BASE%\maps\%NAME%\lm_*.tga" del /q "%BASE%\maps\%NAME%\lm_*.tga"
findstr /X /I /C:"q3map2_%NAME%" "%BASE%\shaders\shaderlist.txt" >nul 2>&1 || exit /b 0
findstr /V /X /I /C:"q3map2_%NAME%" "%BASE%\shaders\shaderlist.txt" > "%BASE%\shaders\shaderlist.tmp"
move /y "%BASE%\shaders\shaderlist.tmp" "%BASE%\shaders\shaderlist.txt" >nul
exit /b 0

:light
rem -gamma / -gridscale are the CS:S pipeline's values, kept as a starting point.
rem Set any of these before running, or in paths.cmd, to retune the lighting.
rem q3map2 ColorToBytes() applies gamma as pow(v/255, 1/gamma)*255, so at 2.2
rem a mid-grey of 128 comes out at 186 and everything above 192 lands within a
rem few values of 255 - which is why bright metal reads as flat white. Lower
rem gamma keeps the midtones where they were. EXPOSURE (0 = off) adds the
rem exponential roll-off 1-exp(-v/E) AFTER gamma, which pulls the top end back
rem under 255 so highlights keep their detail, at the cost of a flatter image.
rem 1.6, not the CS:S pipeline's 2.2: at 2.2 a mid-grey of 128 came out at 186
rem and everything above 192 landed within a few values of 255. cod4mat2shader
rem derives the sun intensity from this number (LIGHT_GAMMA), so changing one
rem without the other puts the sun off target.
rem BOUNCE is q3map2 radiosity, and it is OFF because it does not finish on a
rem map this size. Measured on mp_strike: the direct pass runs on 76 lights,
rem and -bounce 1 turned those into 122,201 diffuse lights (111,693 from brush
rem faces) illuminating 2.45 million luxels - still going after two hours,
rem where the direct pass took 34 seconds. CoD bakes radiosity too, but its
rem own numbers (sunradiosity / radiosityscale in the worldspawn) go into
rem q3map_skyLight instead, at no cost - see cod4_vision.radiosity_scalar.
rem Worth trying on a small map (mp_shipment is 162 clusters against Strike's
rem 1030); expect hours on anything the size of Strike or derail.
if not defined COD4TOSOF2_GAMMA     set "COD4TOSOF2_GAMMA=1.6"
if not defined COD4TOSOF2_GRIDSCALE set "COD4TOSOF2_GRIDSCALE=4.59"
if not defined COD4TOSOF2_EXPOSURE  set "COD4TOSOF2_EXPOSURE=0"
if not defined COD4TOSOF2_BOUNCE    set "COD4TOSOF2_BOUNCE=0"
set "LIGHTTONE=-gamma %COD4TOSOF2_GAMMA% -gridscale %COD4TOSOF2_GRIDSCALE%"
if not "%COD4TOSOF2_EXPOSURE%"=="0" set "LIGHTTONE=%LIGHTTONE% -exposure %COD4TOSOF2_EXPOSURE%"
if not "%COD4TOSOF2_BOUNCE%"=="0" set "LIGHTTONE=%LIGHTTONE% -bounce %COD4TOSOF2_BOUNCE% -bouncegrid"
echo.
echo -light (worldspawn _lightmapscale %LMSCALE%, %LIGHTTONE%) ...
echo (lighting takes a while on big maps - wait for "Done" before starting SOF2)
if defined TEEPY (
    %PYCMD% run_tee "%LIGHTLOG%" "%Q3MAP2%" -game sof2 -fs_basepath "%GAMEROOT%" -light %LIGHTTONE% -filter -patchshadows -shade -samplesize 16 -samples 2 -threads %NUMBER_OF_PROCESSORS% -v "%MAP%.bsp" || exit /b 1
) else (
    "%Q3MAP2%" -game sof2 -fs_basepath "%GAMEROOT%" -light %LIGHTTONE% -filter -patchshadows -shade -samplesize 16 -samples 2 -threads %NUMBER_OF_PROCESSORS% -v "%MAP%.bsp" > "%LIGHTLOG%" 2>&1 || exit /b 1
    type "%LIGHTLOG%"
)
set "PAIRS=0"
if not defined PAIRCHK goto pairsfromlog
rem NOT for /f: the command starts with a quoted .exe path, and cmd mangles
rem the quoting inside for /f ('...'), which silently left PAIRS at 0 and fell
rem through to q3map2's half-sized number. A redirect has no such problem.
%PYCMD% bsp_budget pairs "%MAP%.bsp" -v > "%MAP%_pairs.txt"
if not exist "%MAP%_pairs.txt" goto pairsfromlog
set /p PAIRS=<"%MAP%_pairs.txt"
del /q "%MAP%_pairs.txt" >nul 2>&1
rem must be a plain number, or the step did not run
set "NUMOK=%PAIRS%"
for /f "delims=0123456789" %%x in ("%PAIRS%") do set "NUMOK="
if not defined NUMOK goto pairsfromlog
if "%PAIRS%"=="0" goto pairsfromlog
goto pairsdone
:pairsfromlog
rem q3map2's own line ran about half the real count on every map measured, so
rem double it. Only reached when the .bsp measurement is unavailable.
for /f "tokens=1" %%n in ('findstr /C:"unique lightmap/shader combinations" "%LIGHTLOG%"') do set /a PAIRS=%%n*2
echo NOTE: estimated from q3map2's log (doubled) - the .bsp count did not run.
:pairsdone
echo.
echo shader/lightmap pairs in the .bsp: %PAIRS% (target: %MAXPAIRS% or less, MAX_SHADERS is 2048)
exit /b 0

:fail
echo Compile FAILED.
if not defined COD4TOSOF2_NOPAUSE pause
exit /b 1
