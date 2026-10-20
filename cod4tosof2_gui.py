#!/usr/bin/env python3
"""
cod4tosof2_gui.py - front end for CoD4toSOF2.

Pick a map from ModTools\\map_source and press Convert. That runs:
  1. cod4_collect_assets.py --extract  (from the game's .iwd files + ModTools\\raw)
  2. cod4tosof2.py                     (models -> map -> shaders/textures)

Compile then installs the result into SOF2 and runs q3map2 through
compile_cod4_map.bat.

Output per map:  <output>\\<map>\\  maps\\  models\\cod4\\  shaders\\  textures\\cod4\\
Asset cache:     <output>\\_cache\\<map>\\

Uses CustomTkinter when installed (pip install customtkinter), plain Tk
otherwise. Running from source, every .py of the tool must sit in this
folder; the built CoD4toSOF2.exe carries them itself.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cod4tosof2_run import (FROZEN, NO_WINDOW, VERSION, app_dir,  # noqa: E402
                            child_env, console_python, dispatch, res_dir, step_cmd)

# started as "CoD4toSOF2.exe --step <name> ...": run that step, never open a window
dispatch()

import json          # noqa: E402
import queue         # noqa: E402
import re            # noqa: E402
import subprocess    # noqa: E402
import threading     # noqa: E402

try:
    import customtkinter as ctk
    HAVE_CTK = True
except ImportError:
    HAVE_CTK = False
import tkinter as tk                                   # noqa: E402
from tkinter import filedialog, messagebox, ttk        # noqa: E402

HERE = app_dir()      # where the exe/scripts live - output goes here
RES = res_dir()       # where the bundled .bat lives (temp dir in a onefile exe)
SETTINGS = os.path.join(HERE, "cod4tosof2_settings.json")

# every .py the tool needs next to it when running from source
REQUIRED = ["cod4_collect_assets.py", "cod4tosof2.py", "cod4tosof2_run.py", "cod4tosof2_main.py",
            "cod4tosof2_step.py",
            "iwmap2map.py", "xmodel2md3.py", "cod4mat2shader.py",
            "cod4_names.py", "cod4_collision.py", "cod4_ff.py", "cod4_vision.py",
            "iwi2tga.py", "xmodel_v25.py", "material_iw3.py",
            "sof2_shader.py", "sof2_md3.py"]


# ---------------------------------------------------------------- settings
def _first_dir(*cands):
    for c in cands:
        if c and os.path.isdir(c):
            return os.path.normpath(c)
    return ""


def guess_paths():
    """Likely install folders, so a first start is not four Browse clicks.
    Anything not found is left empty."""
    drives = ["%s:\\" % d for d in "CDEFGH"] if os.name == "nt" else ["/"]
    cod4 = sof2 = ""
    for d in drives:
        cod4 = cod4 or _first_dir(
            os.path.join(d, "Program Files (x86)", "Steam", "steamapps", "common", "Call of Duty 4"),
            os.path.join(d, "SteamLibrary", "steamapps", "common", "Call of Duty 4"),
            os.path.join(d, "Program Files (x86)", "Activision", "Call of Duty 4 - Modern Warfare"),
            os.path.join(d, "COD4"))
        sof2 = sof2 or _first_dir(
            os.path.join(d, "Program Files (x86)", "Steam", "steamapps", "common", "Soldier of Fortune II - Double Helix"),
            os.path.join(d, "SteamLibrary", "steamapps", "common", "Soldier of Fortune II - Double Helix"),
            os.path.join(d, "Soldier of Fortune II - Double Helix"))
    return {
        "cod4": cod4,
        "modtools": _first_dir(os.path.join(cod4, "ModTools")) if cod4 else "",
        "output": os.path.join(HERE, "output"),
        "map": "",
        "sof2base": _first_dir(os.path.join(sof2, "base")) if sof2 else "",
        "q3map2": "",
    }


def load_settings():
    s = guess_paths()
    try:
        with open(SETTINGS, encoding="utf-8") as fh:
            saved = json.load(fh)
        s.update({k: v for k, v in saved.items() if isinstance(v, str)})
    except (OSError, ValueError):
        pass
    return s


def save_settings(s):
    try:
        with open(SETTINGS, "w", encoding="utf-8") as fh:
            json.dump(s, fh, indent=1)
    except OSError:
        pass


# ---------------------------------------------------------------- widgets
if HAVE_CTK:
    ctk.set_appearance_mode("dark")
    ctk.set_default_color_theme("blue")
    Base = ctk.CTk
    Frame, Label, Entry, Button = ctk.CTkFrame, ctk.CTkLabel, ctk.CTkEntry, ctk.CTkButton
else:
    Base = tk.Tk
    Frame, Label, Entry = ttk.Frame, ttk.Label, ttk.Entry

    def Button(parent, width=None, **kw):          # ttk widths are characters, not pixels
        return ttk.Button(parent, width=max(6, (width or 80) // 9), **kw)


class App(Base):
    KEYS = ("cod4", "modtools", "output", "map", "sof2base", "q3map2")

    def __init__(self):
        super().__init__()
        self.title("CoD4toSOF2 \u2014 map converter")
        self.geometry("980x660")
        self.minsize(760, 520)
        self.s = load_settings()
        self.q = queue.Queue()
        self.logfile = None
        self.proc = None
        self.running = False

        self.vars = {k: tk.StringVar(value=self.s.get(k, "")) for k in self.KEYS}

        top = Frame(self)
        top.pack(fill="x", padx=12, pady=(12, 6))
        top.grid_columnconfigure(1, weight=1)
        self._path_row(top, 0, "CoD4 folder", "cod4")
        self._path_row(top, 1, "ModTools folder", "modtools", on_change=self.refresh_maps)
        self._map_row(top, 2)
        self._path_row(top, 3, "Output folder", "output")
        self._path_row(top, 4, "SOF2 base folder", "sof2base")
        self._path_row(top, 5, "q3map2.exe", "q3map2", pick_file=True)

        bar = Frame(self)
        bar.pack(fill="x", padx=12, pady=6)
        self.btn_run = Button(bar, text="Convert", command=self.start, width=130)
        self.btn_run.pack(side="left")
        self.btn_compile = Button(bar, text="Compile", command=self.start_compile, width=110)
        self.btn_compile.pack(side="left", padx=8)
        self.fast = tk.BooleanVar(value=False)
        mk_check = ctk.CTkCheckBox if HAVE_CTK else ttk.Checkbutton
        mk_check(bar, text="fast (test vis)", variable=self.fast).pack(side="left", padx=(0, 8))
        self.btn_stop = Button(bar, text="Stop", command=self.stop, width=80)
        self.btn_stop.pack(side="left")
        self.btn_open = Button(bar, text="Open output", command=self.open_output, width=120)
        self.btn_open.pack(side="left", padx=8)
        self.status = Label(bar, text="ready")
        self.status.pack(side="right")

        self.progress = ttk.Progressbar(self, mode="indeterminate")
        self.progress.pack(fill="x", padx=12)

        logf = Frame(self)
        logf.pack(fill="both", expand=True, padx=12, pady=(6, 12))
        self.log = tk.Text(logf, wrap="none", font=("Consolas", 9), bg="#1e1e1e", fg="#d4d4d4",
                           insertbackground="#d4d4d4", relief="flat")
        ys = ttk.Scrollbar(logf, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=ys.set)
        ys.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)
        self.log.tag_configure("head", foreground="#4fc1ff")
        self.log.tag_configure("warn", foreground="#dcdcaa")
        self.log.tag_configure("err", foreground="#f48771")

        self.refresh_maps()
        self.check_scripts()
        self.after(100, self.pump)
        self.protocol("WM_DELETE_WINDOW", self.on_close)

    # ---- layout helpers
    def _path_row(self, parent, row, label, key, on_change=None, pick_file=False):
        Label(parent, text=label).grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        e = Entry(parent, textvariable=self.vars[key])
        e.grid(row=row, column=1, sticky="ew", pady=4)
        if on_change:
            e.bind("<FocusOut>", lambda _e: on_change())
            e.bind("<Return>", lambda _e: on_change())

        def browse():
            cur = self.vars[key].get()
            if pick_file:
                p = filedialog.askopenfilename(
                    title=label, initialdir=os.path.dirname(cur) or "/",
                    filetypes=[("q3map2", "q3map2.exe"), ("All files", "*.*")])
            else:
                p = filedialog.askdirectory(initialdir=cur or "/")
            if p:
                self.vars[key].set(os.path.normpath(p))
                if on_change:
                    on_change()
        Button(parent, text="Browse", command=browse, width=80).grid(row=row, column=2, padx=(8, 0), pady=4)

    def _map_row(self, parent, row):
        Label(parent, text="Map").grid(row=row, column=0, sticky="w", padx=(0, 10), pady=4)
        if HAVE_CTK:
            self.map_box = ctk.CTkComboBox(parent, variable=self.vars["map"], values=[])
        else:
            self.map_box = ttk.Combobox(parent, textvariable=self.vars["map"], values=[])
        self.map_box.grid(row=row, column=1, sticky="ew", pady=4)
        Button(parent, text="Refresh", command=self.refresh_maps, width=80).grid(row=row, column=2, padx=(8, 0), pady=4)

    def map_source(self):
        return os.path.join(self.vars["modtools"].get(), "map_source")

    def refresh_maps(self):
        ms = self.map_source()
        maps = []
        if os.path.isdir(ms):
            maps = sorted(f for f in os.listdir(ms) if f.lower().endswith(".map"))
        mp = [m for m in maps if m.lower().startswith("mp_")]
        maps = mp + [m for m in maps if m not in mp]           # MP maps first
        self.map_box.configure(values=maps)
        if maps and self.vars["map"].get() not in maps:
            self.vars["map"].set(maps[0])

    def check_scripts(self):
        if FROZEN:                       # the exe carries its own modules
            return True
        missing = [f for f in REQUIRED if not os.path.exists(os.path.join(HERE, f))]
        if missing:
            self.write("Missing next to cod4tosof2_gui.py: %s\n" % ", ".join(missing), "err")
            self.write("  re-extract the CoD4toSOF2 download; all of its .py files "
                       "belong in one folder\n", "err")
        return not missing

    # ---- log
    _ERR = re.compile(r"traceback|\berror\b|exception|failed \(exit|^\s*FAILED |could not start", re.I)
    _WARN = re.compile(r"\bwarn|MISSING [a-z]+ \([1-9]|note:", re.I)

    def write(self, text, tag=None):
        if tag is None:
            if text.startswith(">>") or text.startswith("=="):
                tag = "head"
            elif self._ERR.search(text):
                tag = "err"
            elif self._WARN.search(text):
                tag = "warn"
        self.log.insert("end", text, tag)
        self.log.see("end")

    def pump(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "line":
                    self.write(payload)
                elif kind == "status":
                    self.status.configure(text=payload)
                elif kind == "done":
                    self.finish(payload)
        except queue.Empty:
            pass
        self.after(80, self.pump)

    # ---- run
    def _busy(self, on):
        self.running = on
        for b in (self.btn_run, self.btn_compile):
            b.configure(state="disabled" if on else "normal")
        if on:
            self.progress.start(12)
        else:
            self.progress.stop()
            self.progress.configure(value=0)

    def _remember(self):
        self.s.update({k: self.vars[k].get() for k in self.vars})
        save_settings(self.s)

    def start(self):
        if self.running or not self.check_scripts():
            return
        cod4, mt, out, mp = (self.vars[k].get().strip() for k in ("cod4", "modtools", "output", "map"))
        map_path = os.path.join(self.map_source(), mp)
        for label, p in (("CoD4 folder", cod4), ("ModTools folder", mt), ("map", map_path)):
            if not p or not os.path.exists(p):
                messagebox.showerror("Not found", "%s does not exist:\n%s" % (label, p or "(empty)"))
                return
        self._remember()
        name = os.path.splitext(mp)[0]
        cache = os.path.join(out, "_cache", name)
        dest = os.path.join(out, name)
        steps = [
            ("Collecting assets", step_cmd("collect", [
                "--map", map_path, "--root", cod4, "--root", mt,
                "--prefabs", self.map_source(), "--out", cache, "--extract"])),
            ("Converting", step_cmd("pipeline", [
                "--map", map_path, "--assets", cache, "--prefabs", self.map_source(),
                "--out", dest])),
        ]
        self.log.delete("1.0", "end")
        self._busy(True)
        threading.Thread(target=self.worker, args=(steps, dest, True), daemon=True).start()

    def start_compile(self):
        if self.running:
            return
        mp = self.vars["map"].get().strip()
        name = os.path.splitext(mp)[0]
        out, base, q3 = (self.vars[k].get().strip() for k in ("output", "sof2base", "q3map2"))
        bat = os.path.join(RES, "compile_cod4_map.bat")
        if os.name != "nt":
            messagebox.showerror("Windows only", "Compiling runs q3map2 through a .bat file.")
            return
        for label, p in (("map", name), ("SOF2 base folder", base), ("q3map2.exe", q3),
                         ("compile_cod4_map.bat", bat)):
            if not p or (label != "map" and not os.path.exists(p)):
                messagebox.showerror("Not found", "%s is not set or does not exist:\n%s"
                                     % (label, p or "(empty)"))
                return
        if not os.path.isdir(os.path.join(out, name)):
            messagebox.showerror("Not converted", "No converted output for %s in:\n%s\n\n"
                                 "Press Convert first." % (name, out))
            return
        self._remember()
        env = {
            "Q3MAP2": q3,
            # the .bat wants the folder that CONTAINS base\, not base\ itself
            "GAMEROOT": os.path.dirname(os.path.normpath(base)),
            "OUTDIR": out,
            "COD4TOSOF2_EXE": sys.executable if FROZEN else "",
            # the .bat appends file names straight onto this
            "COD4TOSOF2_DIR": os.path.join(RES, ""),
            # the real exe folder, for paths.cmd - RES is a temp dir in a onefile build
            "COD4TOSOF2_HOME": os.path.join(HERE, ""),
               # no "Press any key" when the GUI drives the .bat
               "COD4TOSOF2_NOPAUSE": "1",
        }
        cmd = [bat, name] + (["fast"] if self.fast.get() else [])
        self.log.delete("1.0", "end")
        self._busy(True)
        threading.Thread(target=self.worker,
                         args=([("Compiling", cmd)], os.path.join(out, name), False, env),
                         daemon=True).start()

    def log_path(self, convert):
        """<output>/<map>_convert.log or _compile.log, with one backup kept."""
        out = self.vars["output"].get().strip() or HERE
        name = os.path.splitext(self.vars["map"].get().strip())[0] or "map"
        path = os.path.join(out, "%s_%s.log" % (name, "convert" if convert else "compile"))
        try:
            os.makedirs(out, exist_ok=True)
            if os.path.exists(path):
                prev = path[:-4] + ".prev.log"
                if os.path.exists(prev):
                    os.remove(prev)
                os.replace(path, prev)
        except Exception:
            pass
        return path

    def emit(self, line):
        """Queue a line for the window and write it to this run's log.

        Called only from the worker thread. The log must NOT be written from
        pump(): that runs on the GUI thread and drains the queue after the
        worker has closed the file, which silently truncated the log.
        """
        self.q.put(("line", line))
        lf = self.logfile
        if lf is not None:
            try:
                lf.write(line)
            except Exception:
                pass

    def worker(self, steps, dest, convert, extra_env=None):
        ok = True
        try:
            self.logfile = open(self.log_path(convert), "w", encoding="utf-8", errors="replace")
        except Exception as e:
            self.logfile = None
            self.emit("(could not open a log file: %s)\n" % e)
        # First line of every log: which build wrote it. The .bat echoes the
        # same string from inside the exe, so if those two disagree the exe
        # being run is not the one that was just built.
        self.emit("CoD4toSOF2 %s%s (%s)\n"
                  % (VERSION, " [exe]" if FROZEN else " [source]", HERE))
        env = child_env()
        if extra_env:
            env.update({k: v for k, v in extra_env.items() if v})
        # Pillow and numpy are bundled in the exe; from source, make sure the
        # interpreter that runs the steps has them
        if convert and not FROZEN:
            py = console_python()
            missing = []
            for mod, pkg in (("PIL", "pillow"), ("numpy", "numpy")):
                chk = subprocess.run([py, "-c", "import " + mod], capture_output=True,
                                     creationflags=NO_WINDOW)
                if chk.returncode != 0:
                    missing.append(pkg)
            if missing:
                self.q.put(("status", "Installing %s..." % ", ".join(missing)))
                self.emit("== %s not found for %s - installing ==\n"
                          % (", ".join(missing), py))
                steps = [("Installing " + ", ".join(missing),
                          [py, "-m", "pip", "install"] + missing)] + list(steps)
        for title, cmd in steps:
            self.q.put(("status", title + "..."))
            self.emit("== %s ==\n" % title)
            try:
                self.proc = subprocess.Popen(cmd, cwd=HERE, stdin=subprocess.DEVNULL,
                                             stdout=subprocess.PIPE,
                                             stderr=subprocess.STDOUT, text=True,
                                             encoding="utf-8", errors="replace",
                                             creationflags=NO_WINDOW, env=env)
                for line in self.proc.stdout:
                    self.emit(line)
                rc = self.proc.wait()
            except OSError as e:
                self.emit("could not start: %s\n" % e)
                rc = -1
            self.proc = None
            if rc != 0:
                self.emit("step '%s' failed (exit %s)\n" % (title, rc))
                ok = False
                break
        if self.logfile:
            try:
                self.logfile.flush()
                self.logfile.close()
            except Exception:
                pass
        self.q.put(("done", (ok, dest, convert)))

    def finish(self, payload):
        ok, dest, convert = payload
        self._busy(False)
        self.status.configure(text="done" if ok else "failed")
        lf = getattr(self, "logfile", None)
        if lf is not None and getattr(lf, "name", None):
            self.write("\nLog: %s\n" % lf.name, "head")
        if ok and convert:
            self.write("\nOutput: %s\n" % dest, "head")
            self.write("Press Compile to install it into SOF2 and run q3map2.\n", "head")
        elif ok:
            self.write("\nCompiled. The .bsp is in <SOF2 base>\\maps.\n", "head")

    def stop(self):
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()
            self.write("\nstopped by user\n", "warn")

    def open_output(self):
        mp = os.path.splitext(self.vars["map"].get())[0]
        d = os.path.join(self.vars["output"].get(), mp)
        if not os.path.isdir(d):
            d = self.vars["output"].get()
        if os.path.isdir(d):
            if os.name == "nt":
                os.startfile(d)                                   # noqa: S606
            else:
                subprocess.Popen(["xdg-open", d])

    def on_close(self):
        self.stop()
        self._remember()
        self.destroy()


def hide_console():
    """The exe is built as a console app so each conversion step keeps a real
    stdout to report through. Hide that console; the log window shows it."""
    if not (FROZEN and os.name == "nt"):
        return
    try:
        import ctypes
        hwnd = ctypes.windll.kernel32.GetConsoleWindow()
        if hwnd:
            ctypes.windll.user32.ShowWindow(hwnd, 0)               # SW_HIDE
    except Exception:
        pass


if __name__ == "__main__":
    hide_console()
    App().mainloop()
