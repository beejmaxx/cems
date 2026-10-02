"""Reproduce the whitepaper's public toy model; no target or checker is used."""
from fractions import Fraction as Q
from pathlib import Path
import argparse
import hashlib
import json
import subprocess
import sys

p = argparse.ArgumentParser()
p.add_argument("repository", type=Path)
p.add_argument("output", type=Path, help="Fresh output directory")
args = p.parse_args()
repo = args.repository.resolve()
args.output.mkdir(mode=0o700)
sys.path.insert(0, str(repo / "src"))
from fixtures import explicit_graph
from swg import write_source

binary = repo / "build/workspace-v1/prep-workspace"
distributions = {"H1": {b"ab": 1, b"ac": 1}, "H2": {b"ab": 1, b"bc": 3}}
commands = []

def invoke(*parts):
    argv = [str(binary), *map(str, parts)]
    r = subprocess.run(argv, capture_output=True, check=True, timeout=30)
    commands.append({"argv": argv, "stdout": r.stdout.decode(), "stderr": r.stderr.decode()})
    return r.stdout.decode()

def inspect(plan, count):
    return [(bytes.fromhex(h), Q(int(s), int(d)))
            for h, s, d in (line.split() for line in invoke("inspect", plan, 0, count).splitlines())]

plans, records = {}, {}
for name, weights, expected in [
    ("initial", {"H1": 3, "H2": 2}, [(b"ab", Q(2, 5)), (b"ac", Q(3, 10)), (b"bc", Q(3, 10))]),
    ("revised", {"H1": 1, "H2": 4}, [(b"bc", Q(3, 5)), (b"ab", Q(3, 10)), (b"ac", Q(1, 10))]),
]:
    source, plan = (args.output / (name + ext) for ext in (".swg", ".plan"))
    write_source(source, {k: explicit_graph(v) for k, v in distributions.items()}, weights)
    records[name] = json.loads(invoke("compile", source, plan))
    actual = inspect(plan, 3)
    assert actual == expected, (name, actual, expected)
    records[name]["rows"] = [[v.decode(), str(q)] for v, q in actual]
    plans[name] = plan

remaining = args.output / "remaining.plan"
records["subtraction"] = json.loads(invoke("subtract", plans["revised"], remaining, plans["initial"], 0, 1))
actual = inspect(remaining, 2)
assert actual == [(b"bc", Q(3, 5)), (b"ac", Q(1, 10))]
records["subtraction"]["rows"] = [[v.decode(), str(q)] for v, q in actual]
assert sum(q for _, q in actual) == Q(7, 10)
assert [q / Q(7, 10) for _, q in actual] == [Q(6, 7), Q(1, 7)]

bounded = args.output / "highest-band.plan"
records["bounded"] = json.loads(invoke("prepare", args.output / "revised.swg", bounded, 1, plans["initial"], 0, 1))
assert inspect(bounded, 1) == [(b"bc", Q(3, 5))]
assert records["bounded"]["complete_remaining_support"] is False

broad = Q(99, 100) / 10**12
narrow = Q(1, 100) / 10
assert broad == Q(99, 10**14) and narrow == Q(1, 1000) and narrow > broad
records["wide_support"] = {"broad_per_string": str(broad), "narrow_per_string": str(narrow)}
records["scope"] = "Public symbolic examples only; the ab completion is simulated, not a receipt. No password checks."
records["binary_sha256"] = hashlib.sha256(binary.read_bytes()).hexdigest()
records["commands"] = commands
(args.output / "results.json").write_text(json.dumps(records, indent=2) + "\n")
print(json.dumps({"passed": True, "results": str((args.output / "results.json").resolve())}))
