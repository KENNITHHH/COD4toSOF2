"""
run_tee.py <logfile> <program> [args...]  - run a program, show its output
live in the console AND write it to <logfile> (like Unix tee). Exit code is
the program's.

compile_cod4_map.bat needs q3map2 -light's output in a file (to read the
"unique lightmap/shader combinations" count), but redirecting it hid all
progress for the minute or more that lighting takes - and a build closed
before -light finished has no lightmaps (everything fullbright in game).
"""
import subprocess
import sys


def main():
    if len(sys.argv) < 3:
        sys.exit(__doc__)
    log, cmd = sys.argv[1], sys.argv[2:]
    with open(log, "w", encoding="utf-8", errors="replace") as fh:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for raw in iter(p.stdout.readline, b""):
            line = raw.decode("utf-8", "replace")
            sys.stdout.write(line)
            sys.stdout.flush()
            fh.write(line)
        sys.exit(p.wait())


if __name__ == "__main__":
    main()
