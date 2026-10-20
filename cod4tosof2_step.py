#!/usr/bin/env python3
"""
cod4tosof2_step.py - run one helper by name:  python cod4tosof2_step.py <step> [args...]

compile_cod4_map.bat needs the same command shape whether it is driving the
built CoD4toSOF2.exe (which takes "--step <name> ...") or a plain Python
install. This gives the source side that shape, so the .bat sets one variable
and never has to append ".py" or switch quoting.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from cod4tosof2_run import STEPS, dispatch  # noqa: E402

if __name__ == "__main__":
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help"):
        sys.exit("usage: cod4tosof2_step.py <step> [args...]\nsteps: %s"
                 % ", ".join(sorted(STEPS)))
    # dispatch() reads argv as [prog, "--step", name, ...]
    dispatch([sys.argv[0], "--step"] + sys.argv[1:])
