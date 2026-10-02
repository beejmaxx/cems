"""Bounded native-operation helpers for generated regression tests."""

from pathlib import Path

from layout import BUILD

import shutil, subprocess, json, time

BIN = BUILD / "prep"

CAP = 256 * 1024**2

RESERVE = 10 * 1024**3

def disk_guard(out, reservation=0):
    used = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    if used + reservation > CAP or shutil.disk_usage(out).free < RESERVE + reservation:
        raise RuntimeError("experiment disk cap/reserve")
    return used

def invoke(*args):
    started = time.monotonic()
    if args and args[0] in {"compile", "prepare", "subtract"}:
        # Reserve the native writer's maximum file size before launching it.
        disk_guard(Path(args[2]).parent, 64 * 1024**2)
    p = subprocess.run([str(BIN), *map(str, args)], capture_output=True, text=True, timeout=100)
    if p.returncode:
        raise RuntimeError(p.stderr.strip())
    result = json.loads(p.stdout)
    result["process_wall_seconds"] = time.monotonic() - started
    return result
