#!/usr/bin/env python3
"""
cod4tosof2_main.py - the exe's entry point.

This exists so that a failure while STARTING the GUI is visible. The exe is a
console app whose console gets hidden once the window is up; without this
wrapper, anything that went wrong during import or __init__ printed a
traceback into a console that then vanished, and the user saw a window flash
and nothing else.

Everything here runs before tkinter is touched, so it keeps to os/sys and
imports nothing that could be the thing that is broken.

    CoD4toSOF2.exe              normal start
    CoD4toSOF2.exe --diag       print what is bundled and what imports, then wait
    CoD4toSOF2.exe --console    start the GUI but leave the console visible
    CoD4toSOF2.exe --step NAME  run one conversion step (used by the GUI/.bat)
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cod4tosof2_run import FROZEN, app_dir, dispatch, res_dir  # noqa: E402

# "CoD4toSOF2.exe --step <name> ...": run that step and exit, no window
dispatch()

CRASHLOG = os.path.join(app_dir(), "cod4tosof2_crash.log")


def show_console():
    """Un-hide the console so a message is readable. No-op off Windows."""
    if os.name != "nt":
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 5)        # SW_SHOW
            ctypes.windll.user32.SetForegroundWindow(hwnd)
    except Exception:
        pass


def wait():
    """Keep the window up so the text can be read. Never raise."""
    try:
        sys.stdout.flush()
    except Exception:
        pass
    try:
        input("\n-- press Enter to close --")
    except Exception:
        try:
            os.system("pause >nul 2>&1" if os.name == "nt" else "")
        except Exception:
            pass


def report_crash():
    """Print the traceback, save it next to the exe, and wait."""
    import traceback
    text = traceback.format_exc()
    head = ("CoD4toSOF2 failed to start.\n"
            "Python %s\nexe %s\nbundle %s\n\n" % (sys.version, sys.executable, res_dir()))
    show_console()
    sys.stderr.write("\n" + head + text + "\n")
    try:
        with open(CRASHLOG, "w", encoding="utf-8") as fh:
            fh.write(head + text)
        sys.stderr.write("saved to %s\n" % CRASHLOG)
    except Exception as e:
        sys.stderr.write("(could not write %s: %s)\n" % (CRASHLOG, e))
    wait()


def diag():
    """Print everything worth knowing when the exe will not start."""
    show_console()
    p = sys.stdout.write
    p("CoD4toSOF2 diagnostics\n%s\n" % ("-" * 60))
    p("python      %s\n" % sys.version.replace("\n", " "))
    p("frozen      %s\n" % FROZEN)
    p("executable  %s\n" % sys.executable)
    p("exe folder  %s\n" % app_dir())
    p("bundle dir  %s\n" % res_dir())
    p("argv        %r\n" % (sys.argv,))
    for name in ("TCL_LIBRARY", "TK_LIBRARY", "_PYI_APPLICATION_HOME_DIR",
                 "_PYI_ARCHIVE_FILE", "_PYI_PARENT_PROCESS_LEVEL"):
        p("%-11s %s\n" % (name, os.environ.get(name, "(unset)")))

    p("\nbundled files it needs\n")
    for f in ("compile_cod4_map.bat", "paths.cmd.example", "README.md"):
        full = os.path.join(res_dir(), f)
        p("  %-22s %s\n" % (f, "ok" if os.path.exists(full) else "MISSING"))

    p("\ntop of the bundle folder\n")
    try:
        names = sorted(os.listdir(res_dir()))
        for n in names[:40]:
            p("  %s\n" % n)
        if len(names) > 40:
            p("  ... %d more\n" % (len(names) - 40))
    except Exception as e:
        p("  cannot list: %s\n" % e)

    p("\nimports\n")
    for mod in ("tkinter", "tkinter.ttk", "tkinter.filedialog", "_tkinter",
                "customtkinter", "PIL", "PIL.Image", "numpy",
                "iwmap2map", "xmodel2md3", "cod4mat2shader", "cod4_collect_assets"):
        try:
            __import__(mod)
            p("  %-22s ok\n" % mod)
        except Exception as e:
            p("  %-22s FAILED  %s: %s\n" % (mod, type(e).__name__, e))

    p("\ntk really starting up\n")
    try:
        import tkinter
        r = tkinter.Tk()
        p("  Tk %s, window created ok\n" % r.tk.call("info", "patchlevel"))
        r.destroy()
    except Exception as e:
        p("  FAILED  %s: %s\n" % (type(e).__name__, e))
    wait()


def main():
    if "--diag" in sys.argv:
        diag()
        return 0
    keep = "--console" in sys.argv
    try:
        import cod4tosof2_gui as gui
        app = gui.App()
    except BaseException:                      # import or __init__ blew up
        report_crash()
        return 1
    if not keep:
        gui.hide_console()
    try:
        app.mainloop()
    except BaseException:
        report_crash()
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
