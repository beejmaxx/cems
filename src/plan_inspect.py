"""Browse ranked candidates in existing plans. Never runs a password checker."""
import argparse
from collections import Counter
from dataclasses import dataclass, field
from fractions import Fraction
import json
from pathlib import Path
import re
import subprocess

from model_workflow import BUNDLE, CORE, METADATA_CAP, inspect, read_json
from runner import execute, require, sha
from workspace import Workspace

MAX_WINDOWS = 200
MAX_ROWS = 5000
MAX_RANK = 2**64 - 1


def rank_number(value):
    """Decimal suffixes and fractions use integer/rational arithmetic only."""
    compact = value.replace(",", "").replace("_", "").lower()
    match = re.fullmatch(r"([0-9]+(?:\.[0-9]+)?)([kmbtq]?)", compact)
    if not match:
        raise argparse.ArgumentTypeError("use a rank such as 1, 100m, 1.5t or 200t")
    power = {"": 0, "k": 3, "m": 6, "b": 9, "t": 12, "q": 15}[match[2]]
    result = Fraction(match[1]) * 10**power
    if result.denominator != 1 or not 1 <= result <= MAX_RANK:
        raise argparse.ArgumentTypeError(f"rank must be a whole number from 1 to {MAX_RANK:,}")
    return int(result)


@dataclass
class Plan:
    path: Path
    view: str
    digest: str | None = None
    core_digest: str | None = None
    count: int | None = None
    history_policy: str | None = None


@dataclass
class Model:
    name: str
    key: str
    status: str
    plans: dict = field(default_factory=dict)


def catalog(ws):
    """Read manifests at the prepared-model collection level, never archives."""
    prepared = ws.resolve("prepared-models")
    profiles = ws.profiles() if ws.document.get("profiles") else {}
    selected = {p.resolve(): name for name, p in profiles.items()}
    manifests = set(prepared.glob("*/manifest.json"))
    manifests.update(p / "manifest.json" for p in profiles.values())
    entries, previews = [], []
    for path in sorted(manifests):
        ws.resolve(str(path.relative_to(ws.root)))
        if not path.is_file():
            continue
        doc = read_json(path, METADATA_CAP)
        directory = path.parent
        if doc.get("schema") == BUNDLE:
            name = selected.get(directory.resolve(), directory.name)
            entry = Model(name, str(directory.relative_to(ws.root)),
                          "workspace profile" if directory.resolve() in selected else "prepared")
            entry.plans["full"] = Plan(ws.resolve(str((directory / "model.plan").relative_to(ws.root))),
                "full", doc["files"]["model.plan"], doc["core"]["sha256"],
                int(doc["native_compile"]["candidates"]))
            if doc.get("review", {}).get("status") == "proposal":
                entry.status = "proposal"
                remaining = doc["review"].get("remaining")
                if remaining:
                    entry.plans["remaining"] = Plan(ws.resolve(str((directory / "remaining.plan").relative_to(ws.root))),
                        "remaining", remaining["sha256"], doc["core"]["sha256"],
                        int(remaining["count"]), remaining["history_policy"])
            entries.append(entry)
        elif doc.get("schema") in {"history-aware-personal-preview-v1", "history-aware-personal-preview-v2"}:
            previews.append((directory, doc))
        elif (doc.get("status") == "passed" and isinstance(doc.get("profiles"), list)
              and doc.get("core_sha256") and doc["profiles"]
              and all("full_plan_sha256" in row for row in doc["profiles"])):
            for row in doc["profiles"]:
                name = row["profile"]
                require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9_-]+", name), "invalid profile name")
                entry = Model(name, f"{directory.name}/{name}",
                              "proposal" if doc.get("adopted") is False else "prepared")
                for view, filename, digest, count in (
                    ("full", "model.plan", "full_plan_sha256", "model_count"),
                    ("remaining", "remaining.plan", "remaining_plan_sha256", "remaining_count"),
                ):
                    candidate = ws.resolve(str((directory / name / filename).relative_to(ws.root)))
                    entry.plans[view] = Plan(candidate, view, row[digest], doc["core_sha256"],
                                             int(row[count]), doc.get("history_policy") if view == "remaining" else None)
                entries.append(entry)

    # Only the explicitly selected preview supplies history for workspace profiles.
    configured_preview = ws.document.get("resources", {}).get("preview")
    for directory, doc in previews:
        if not configured_preview or directory.resolve() != ws.resolve(configured_preview).resolve():
            continue
        for row in doc["profiles"]:
            for entry in entries:
                if entry.status != "workspace profile" or entry.name != row["profile"]:
                    continue
                full = entry.plans["full"]
                require(row["model_plan_sha256"] == full.digest and doc["core_sha256"] == full.core_digest,
                        f"selected preview does not match {entry.name}; use an explicit plan path")
                candidate = ws.resolve(str((directory / (entry.name + "-remaining.plan")).relative_to(ws.root)))
                entry.plans["remaining"] = Plan(candidate, "remaining", row["remaining_plan_sha256"],
                    doc["core_sha256"], int(row["remaining_candidates"]), doc.get("evidence_policy"))
    return sorted(entries, key=lambda item: (item.name, item.key))


def choose(selector, full=False):
    path = Path(selector).expanduser()
    if path.is_file():
        require(not full, "--full is for named models; an explicit file is inspected as supplied")
        return Model(path.name, str(path.resolve()), "explicit plan"), Plan(path.resolve(), "explicit")
    if path.is_dir():
        filename = "model.plan" if full or not (path / "remaining.plan").is_file() else "remaining.plan"
        require((path / filename).is_file(), f"no {filename} in {path}")
        return Model(path.name, str(path.resolve()), "explicit directory"), Plan(
            (path / filename).resolve(), "full" if filename == "model.plan" else "remaining")
    if path.is_absolute() or selector.startswith(("./", "../", "~/")) or selector.endswith(".plan"):
        raise ValueError(f"plan does not exist: {path}")
    entries = catalog(Workspace())
    matches = [entry for entry in entries if entry.key == selector]
    if not matches:
        matches = [entry for entry in entries if entry.name == selector]
    if len(matches) > 1:
        raise ValueError("ambiguous model; choose " + " or ".join(entry.key for entry in matches))
    if not matches:
        raise ValueError(f"unknown model {selector!r}; run recollect models")
    entry = matches[0]
    view = "full" if full or "remaining" not in entry.plans else "remaining"
    return entry, entry.plans[view]


def windows(args, total):
    require(1 <= args.count <= 128, "--count must be 1..128")
    if args.every is not None:
        require(args.to is not None, "--every requires --to (for example --every 1t --to 10t)")
        start = args.start if args.start is not None else args.every
        require(start <= args.to, "--from must not exceed --to")
        number = (args.to - start) // args.every + 1
        require(number <= MAX_WINDOWS, f"at most {MAX_WINDOWS} sampling points per command")
        ranks = list(range(start, args.to + 1, args.every))
    else:
        require(args.start is None and args.to is None, "--from and --to require --every")
        ranks = args.at or [1]
    ranks = list(dict.fromkeys(ranks))
    require(len(ranks) <= MAX_WINDOWS, f"at most {MAX_WINDOWS} sampling points per command")
    require(len(ranks) * args.count <= MAX_ROWS, f"at most {MAX_ROWS:,} displayed candidates per command")
    for rank in ranks:
        require(rank <= total, f"rank {rank:,} exceeds this plan's {total:,} candidates")
    return [(rank, min(args.count, total - rank + 1)) for rank in ranks]


def quoted(value):
    # Escape bytes rather than emitting terminal controls or losing invalid UTF-8.
    result = []
    escapes = {9: r"\t", 10: r"\n", 13: r"\r", 34: r'\"', 92: r"\\"}
    for byte in value:
        result.append(escapes.get(byte, chr(byte) if 32 <= byte < 127 else f"\\x{byte:02x}"))
    return '"' + "".join(result) + '"'


def sample(entry, plan, args):
    require(CORE.is_file(), "native engine missing; run make build")
    require(plan.path.is_file(), f"plan missing: {plan.path}")
    digest = sha(plan.path)
    require(not plan.digest or digest == plan.digest, "plan differs from its manifest; refusing mislabeled results")
    require(not plan.core_digest or sha(CORE) == plan.core_digest,
            "native engine differs from the model's recorded engine")
    info = json.loads(execute([CORE, "describe", plan.path]))
    total = int(info["candidates"])
    require(plan.count is None or plan.count == total, "plan count differs from its manifest")
    selections = windows(args, total)
    result = {"schema": "ranked-plan-inspection-v1", "model": entry.name, "status": entry.status,
              "plan": str(plan.path), "plan_sha256": digest, "view": plan.view,
              "candidates": total, "one_based_ranks": True, "history_policy": plan.history_policy,
              "complete_remaining_support": info["complete_remaining_support"], "windows": []}
    for rank, count in selections:
        candidates = inspect(CORE, plan.path, rank - 1, count)
        require(len(candidates) == count, "native inspection returned an incomplete window")
        rows = [{"rank": rank + offset, "candidate": value.decode("ascii") if value.isascii() else None,
                 "display": quoted(value), "hex": value.hex(), "score": str(score)}
                for offset, (value, score) in enumerate(candidates)]
        result["windows"].append({"at": rank, "count": count, "rows": rows})
    require(sha(plan.path) == digest, "plan changed during inspection; discard this sample")
    return result


def print_sample(result, scores=False):
    print(f"{result['model']} | {result['status']} | {result['view']} plan | {result['candidates']:,} candidates")
    print("Ranks start at 1. Inspection only; no checking or adoption.")
    if result["view"] == "remaining":
        print("Ranks use this saved plan's historical exclusions; no live campaign state is consulted.")
    if not result["complete_remaining_support"]:
        print("This plan is a prepared prefix; further model candidates remain unprepared.")
    if scores:
        print("Model scores are rounded below; --json preserves exact fractions. They are not measured recovery odds.")
    for window in result["windows"]:
        first, last = window["at"], window["at"] + window["count"] - 1
        print(f"\nRanks {first:,}–{last:,}")
        width = len(f"{last:,}")
        for row in window["rows"]:
            score = f"  {float(Fraction(row['score'])):.6e}" if scores else ""
            print(f"  {row['rank']:>{width},}{score}  {row['display']}")


def list_models(as_json=False):
    entries = catalog(Workspace())
    counts = Counter(entry.name for entry in entries)
    rows = []
    for entry in entries:
        view = "remaining" if "remaining" in entry.plans else "full"
        rows.append({"model": entry.name if counts[entry.name] == 1 else entry.key,
                     "status": entry.status, "default_view": view,
                     "candidates": entry.plans[view].count})
    if as_json:
        print(json.dumps(rows, indent=2))
        return
    if not rows:
        print("No prepared models found. Inspect an explicit .plan path or compile a model first.")
        return
    width = max(5, *(len(row["model"]) for row in rows))
    print(f"{'MODEL':<{width}}  {'STATUS':<17}  {'VIEW':<9}  CANDIDATES (manifest)")
    for row in rows:
        print(f"{row['model']:<{width}}  {row['status']:<17}  {row['default_view']:<9}  {row['candidates']:,}")
    print("\nrecollect inspect MODEL --at 1 1t 2t 100t 200t --count 5")


def main():
    parser = argparse.ArgumentParser(prog="recollect inspect", description=__doc__, epilog=(
        "Examples: recollect inspect combined_high --at 1t 2t 100t 200t\n"
        "          recollect inspect combined_high --every 1t --to 10t --scores\n"
        "          recollect inspect /path/to/model.plan --at 1 --json\n"
        "Ranks are one-based. Suffixes: k=thousand, m=million, b=billion, t=trillion, q=quadrillion."),
        formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("model", nargs="?", help="model name from 'recollect models', or a .plan path")
    parser.add_argument("--list", action="store_true", help="list available models")
    positions = parser.add_mutually_exclusive_group()
    positions.add_argument("--at", nargs="+", type=rank_number, metavar="RANK", help="sample these ranks (default: 1)")
    positions.add_argument("--every", type=rank_number, metavar="STEP", help="sample at fixed rank intervals; requires --to")
    parser.add_argument("--from", dest="start", type=rank_number, metavar="RANK", help="first interval rank (default: STEP)")
    parser.add_argument("--to", type=rank_number, metavar="RANK", help="last allowed interval rank, inclusive")
    parser.add_argument("--count", type=int, default=5, help="consecutive candidates per rank, 1..128 (default: 5)")
    parser.add_argument("--full", action="store_true", help="inspect before history exclusions; default uses remaining when available")
    parser.add_argument("--scores", action="store_true", help="display each candidate's model score")
    parser.add_argument("--json", action="store_true", help="machine-readable output with exact scores and candidate hex")
    args = parser.parse_args()
    if args.list and (args.model or args.at or args.every or args.start or args.to or args.full or args.scores or args.count != 5):
        parser.error("--list accepts only --json; sampling options require a model")
    if not args.list and args.model is None:
        parser.error("choose a model; run recollect models")
    try:
        if args.list:
            list_models(args.json)
        else:
            entry, plan = choose(args.model, args.full)
            result = sample(entry, plan, args)
            if args.json:
                print(json.dumps(result, indent=2))
            else:
                print_sample(result, args.scores)
    except (OSError, ValueError, RuntimeError, KeyError, subprocess.TimeoutExpired) as error:
        parser.exit(2, f"{parser.prog}: {error}\n")


if __name__ == "__main__":
    main()
