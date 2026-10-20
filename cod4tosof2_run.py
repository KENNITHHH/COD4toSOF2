#!/usr/bin/env python3
"""
cod4tosof2_run.py - how CoD4toSOF2 starts its conversion steps.

Each step is a separate process: a step that fails or is stopped cannot take
the GUI with it, and its output is read back over a pipe.

Run from source, a step is  python <step>.py ...
Run from the built CoD4toSOF2.exe there is no python.exe and no .py files, so
the exe starts a copy of ITSELF with  --step <name> ...  and dispatch() below
turns that back into the step's own main().
"""
import os
import sys

# step name -> module (also the .py file name when running from source)
STEPS = {
    "collect":  "cod4_collect_assets",
    "pipeline": "cod4tosof2",
    "map":      "iwmap2map",
    "models":   "xmodel2md3",
    "shaders":  "cod4mat2shader",
}
# compile helpers, called by compile_cod4_map.bat; named after their module so
# the .bat can pass one name whether it runs python or the exe
for _m in ("bsp_fix_leafbounds", "map_set_key", "bsp_static_pvs", "run_tee",
           "bsp_budget"):
    STEPS[_m] = _m

# Printed at the top of every Convert and Compile, and echoed by the .bat
# (which is bundled inside the exe, so the two always agree unless an older
# exe is being run). Two rounds of "that fix is not in the log" cost an
# evening each, so bump this with every release.
VERSION = "2026.10.20.6"

FROZEN = getattr(sys, "frozen", False)
NO_WINDOW = 0x08000000 if os.name == "nt" else 0          # CREATE_NO_WINDOW


def app_dir():
    """Folder the tool was started from: the one holding CoD4toSOF2.exe when
    built, the one holding the .py files when run from source."""
    if FROZEN:
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def res_dir():
    """Folder holding the bundled data files (compile_cod4_map.bat and the
    rest). A onefile exe unpacks them to a temp folder, so this is NOT the
    folder the exe sits in - use app_dir() for anything the user should see."""
    if FROZEN:
        return getattr(sys, "_MEIPASS", app_dir())
    return os.path.dirname(os.path.abspath(__file__))


def console_python():
    """pythonw.exe has no stdout, so a step started with it prints nothing
    (and its traceback is lost). Use python.exe from the same install."""
    exe = sys.executable
    if os.path.basename(exe).lower() == "pythonw.exe":
        alt = os.path.join(os.path.dirname(exe), "python.exe")
        if os.path.exists(alt):
            return alt
    return exe


def step_cmd(step, args):
    """Command line that runs one conversion step."""
    if step not in STEPS:
        raise KeyError(step)
    if FROZEN:
        return [sys.executable, "--step", step] + [str(a) for a in args]
    return [console_python(), "-u", os.path.join(app_dir(), STEPS[step] + ".py")] + \
           [str(a) for a in args]


def child_env():
    return dict(os.environ, PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8")


def _restore_stdio():
    """A windowed build has no sys.stdout, which would silence every print()
    in a step. The pipe handle the parent gave us is still there, so rebuild
    the streams from it. No-op in a console build (the normal case)."""
    if sys.stdout is not None and sys.stderr is not None:
        return
    try:
        import msvcrt
        import ctypes
        for fileno, std, name in ((1, -11, "stdout"), (2, -12, "stderr")):
            if getattr(sys, name) is not None:
                continue
            h = ctypes.windll.kernel32.GetStdHandle(std)
            if not h or h == ctypes.c_void_p(-1).value:
                setattr(sys, name, open(os.devnull, "w"))
                continue
            fd = msvcrt.open_osfhandle(h, os.O_WRONLY)
            setattr(sys, name, os.fdopen(fd, "w", buffering=1,
                                         encoding="utf-8", errors="replace"))
    except Exception:
        for name in ("stdout", "stderr"):
            if getattr(sys, name) is None:
                setattr(sys, name, open(os.devnull, "w"))


def dispatch(argv=None):
    """Built exe started as '--step <name> ...': run that step and exit.
    Returns normally when this is an ordinary start, so the caller goes on
    to open the GUI."""
    argv = list(sys.argv if argv is None else argv)
    if len(argv) < 3 or argv[1] != "--step":
        return
    name = argv[2]
    if name not in STEPS:
        sys.exit("unknown step: %s" % name)
    _restore_stdio()
    module = STEPS[name]
    sys.argv = [module + ".py"] + argv[3:]
    # run_module reproduces 'python <module>.py' exactly, including modules
    # whose work sits under their own "if __name__ == '__main__'"
    import runpy
    try:
        runpy.run_module(module, run_name="__main__", alter_sys=True)
    except SystemExit:
        raise
    except Exception:
        import traceback
        traceback.print_exc()
        sys.exit(1)
    sys.exit(0)
