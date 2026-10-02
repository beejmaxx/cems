"""Bounded native inspection helpers shared by experiments and regression tests."""
from fractions import Fraction as Q
import os
from pathlib import Path
import subprocess
from layout import BUILD

BIN = Path(os.environ.get("PREP_BINARY", str(BUILD / "prep"))).resolve()


def run(*args, **kwargs):
    return subprocess.run([str(BIN), *map(str, args)], capture_output=True, timeout=100, check=True, **kwargs)


def rows(path, start, count):
    text = run("inspect", path, start, count).stdout.decode()
    result = []
    for line in text.splitlines():
        value, score, denominator = line.split(" ")
        result.append((bytes.fromhex(value), Q(int(score), int(denominator))))
    return result
