# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller spec for CoD4toSOF2.

Build (Windows, from this folder):
    build_exe.bat
or by hand:
    py -m pip install pyinstaller pillow numpy customtkinter
    py -m PyInstaller --noconfirm CoD4toSOF2.spec

Produces a single dist\\CoD4toSOF2.exe. Ship that one file; the machine that
runs it needs no Python and no other files beside it.

Notes
  * console=True on purpose. Every conversion step runs as a separate process
    and reports over a pipe, which needs a real stdout. The GUI hides the
    console window at startup (hide_console in cod4tosof2_gui.py).
  * onefile. The exe starts copies of itself for each step, which sounds like
    it would unpack the archive again each time - it does not. The bootloader
    passes _PYI_APPLICATION_HOME_DIR down to the child and only the first
    process (PYI_PROCESS_LEVEL_PARENT) runs _pyi_main_onefile_parent, so the
    steps reuse the folder the parent already unpacked. The one real cost is
    startup, once per launch.
  * compile_cod4_map.bat is bundled inside the exe and read from sys._MEIPASS
    (res_dir() in cod4tosof2_run.py), not from next to the exe. The .bat picks
    up paths.cmd from COD4TOSOF2_HOME, which is the folder the exe sits in.
  * The conversion steps are imported by name at run time (cod4tosof2_run.py
    STEPS), so they are listed as hidden imports here.
  * The entry point is cod4tosof2_main.py, not the GUI module. It catches a
    failure during import or App() and prints it instead of letting the
    console vanish. "CoD4toSOF2.exe --diag" prints what is bundled.
"""
import os

# modules started as "CoD4toSOF2.exe --step <name>" - no static import reaches them
STEP_MODULES = [
    "cod4_collect_assets", "cod4tosof2", "iwmap2map", "xmodel2md3",
    "cod4mat2shader", "bsp_fix_leafbounds", "map_set_key", "bsp_static_pvs",
    "run_tee", "cod4tosof2_step", "bsp_budget",
]
# imported by the steps above
LIB_MODULES = [
    "cod4_names", "cod4_collision", "cod4_ff", "cod4_vision", "iwi2tga", "material_iw3",
    "xmodel_v25", "sof2_shader", "sof2_md3",
]

hidden = (STEP_MODULES + LIB_MODULES
          + ["cod4tosof2_gui", "PIL.Image", "PIL._tkinter_finder", "numpy"])

# files the running tool reads back out of itself: fetched with res_dir()
datas = [(f, ".") for f in ("compile_cod4_map.bat", "paths.cmd.example", "README.md",
                            "LICENSE", "THIRD-PARTY-LICENSES.txt")
         if os.path.exists(f)]

try:                                     # themes/fonts, only if it is installed
    from PyInstaller.utils.hooks import collect_all
    _d, _b, _h = collect_all("customtkinter")
    datas += _d
    hidden += _h
except Exception:
    pass

a = Analysis(
    ["cod4tosof2_main.py"],
    pathex=[os.path.abspath(".")],
    binaries=[],
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "scipy", "pandas", "pytest", "setuptools", "IPython"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# set COD4TOSOF2_BOOTDEBUG=1 before building to make the bootloader itself
# log what it extracts and where - the only way to see a failure that happens
# before Python starts. There is no runtime switch for it.
BOOTDEBUG = bool(os.environ.get("COD4TOSOF2_BOOTDEBUG"))

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="CoD4toSOF2",
    debug="bootloader" if BOOTDEBUG else False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,                 # see the note at the top
    disable_windowed_traceback=False,
    icon=os.path.abspath("CoD4toSOF2.ico") if os.path.exists("CoD4toSOF2.ico") else None,
)
