"""Editable construction inputs -> immutable native plan -> eligible preview.

The existing emitter compiler is an OFFLINE dependency only. The candidate hot
path and campaign history still use the existing C++ worker and pinned runner.
This module never opens a target, chooses a secret, or performs password checks.
"""
from layout import BUILD, source as source_path
import argparse
import copy
from fractions import Fraction as Q
import json
import os
from pathlib import Path
import shutil
import tempfile
import time

from checkpoint_runner import CheckpointCampaign, parse
from swg import probability, write_source
from runner import CAP, HERE, PLAN_CAP, RESERVE, disk_guard, execute, require, sha

ROOT = HERE  # Source identities are repository-relative.
SCHEMA = "search-constructions-v1"
BUNDLE = "construction-bundle-v1"
INPUT_CAP = 2 * 1024**2
METADATA_CAP = 16 * 1024**2
CORE = BUILD / "prep-workspace"


def read_json(path, cap=INPUT_CAP):
    with Path(path).open("rb") as stream:
        raw = stream.read(cap + 1)
    require(len(raw) <= cap, "input size limit")
    return parse(raw)


def save_new(path, value):
    raw = (json.dumps(value, indent=2, sort_keys=True, allow_nan=False) + "\n").encode()
    require(len(raw) <= METADATA_CAP, "metadata size limit")
    with Path(path).open("xb") as stream:
        os.chmod(path, 0o600)
        stream.write(raw)
        stream.flush()
        os.fsync(stream.fileno())


def budget(root, extra=0, limit=CAP):
    root = Path(root)
    used = sum(p.stat().st_size for p in root.rglob("*") if p.is_file()) if root.exists() else 0
    require(0 < limit <= CAP and used + extra <= limit, "workflow storage budget exceeded")
    require(shutil.disk_usage(root if root.exists() else root.parent).free >= RESERVE + extra,
            "workflow free disk reserve")
    return {"retained_bytes": used, "limit_bytes": limit, "free_disk_reserve_bytes": RESERVE,
            "reserved_next_operation_bytes": extra}


def expand(spec):
    """Only named-component substitution; probability semantics stay in emitter_v1.

    A component reference copies an expression (independent choices). To choose
    once and reuse, explicitly bind it with `let`, then use the existing `ref`.
    """
    require(type(spec) is dict and spec.get("schema") == SCHEMA, "unknown editable model schema")
    require(set(spec) <= {"schema", "description", "components", "hypotheses"}, "unknown model field")
    components, hypotheses = spec.get("components", {}), spec.get("hypotheses")
    require(type(components) is dict and len(components) <= 128 and
            all(type(k) is str and k for k in components), "invalid components")
    require(type(hypotheses) is list and 0 < len(hypotheses) <= 32, "hypothesis count limit")
    visits = 0

    def visit(value, stack=(), depth=0):
        nonlocal visits
        visits += 1
        require(visits <= 100_000 and depth <= 128, "component expansion limit")
        if type(value) is dict:
            if value.get("op") == "component":
                require(set(value) == {"op", "name"}, "component fields")
                name = value["name"]
                require(type(name) is str and name in components, "unknown component")
                require(name not in stack, "cyclic component reference")
                return visit(components[name], (*stack, name), depth + 1)
            return {k: visit(v, stack, depth + 1) for k, v in value.items()}
        if type(value) is list:
            return [visit(v, stack, depth + 1) for v in value]
        return value

    # Reject broken definitions even if a current branch does not happen to use them.
    for name in components:
        visit({"op": "component", "name": name})
    result, ids = [], set()
    for branch in hypotheses:
        require(type(branch) is dict and set(branch) <= {"id", "weight", "root", "evidence"},
                "unknown hypothesis field")
        name = branch.get("id")
        require(type(name) is str and name and name not in ids, "duplicate/invalid hypothesis id")
        ids.add(name)
        value = branch.get("weight")
        require(type(value) in (int, str), "use integer or fraction/decimal-string weights, not floats")
        weight = Q(value)
        require(weight > 0 and max(weight.numerator.bit_length(), weight.denominator.bit_length()) <= 128,
                "positive bounded hypothesis weight required")
        require("root" in branch, "missing construction root")
        result.append({"id": name, "weight": str(weight), "root": visit(branch["root"]),
                       "evidence": copy.deepcopy(branch.get("evidence", {}))})
    return result


def build_bundle(spec, output, *, core=CORE, storage_limit=CAP):
    """Bounded offline preparation; no candidate database or target input."""
    from emitter_v1 import automaton, model

    branches = expand(spec)
    output, core = Path(output).resolve(), Path(core).resolve(strict=True)
    require(not output.exists(), "bundle destination exists; use a new version directory")
    budget(output, 2 * PLAN_CAP + METADATA_CAP, storage_limit)
    output.mkdir(mode=0o700)
    began = time.monotonic()
    save_new(output / "inputs.json", spec)
    graphs, weights, details = {}, {}, {}
    for branch in branches:
        compiled = automaton.compile_probability(model.model(branch["root"], evidence=branch["evidence"]))
        name = branch["id"]
        graphs[name], weights[name] = compiled.payload(), Q(branch["weight"])
        details[name] = {"weight": branch["weight"], "conditional_distinct_count": compiled.count,
                         "evidence": branch["evidence"], "compiler_stats": compiled.stats}
    total = sum(weights.values())
    for name in details:
        details[name]["normalized_hypothesis_weight"] = str(weights[name] / total)
    save_new(output / "branches.json", {"graphs": graphs, "hypotheses": details})
    budget(output, 2 * PLAN_CAP, storage_limit)
    exported = write_source(output / "source.swg", graphs, weights)
    budget(output, PLAN_CAP, storage_limit)
    # Native compiler sums overlapping branches and establishes global ordering.
    info = json.loads(execute([core, "compile", output / "source.swg", output / "model.plan"],
                              file_cap=PLAN_CAP))
    os.chmod(output / "model.plan", 0o600)
    manifest = {"schema": BUNDLE, "input_schema": SCHEMA,
                "ordering": "exact summed string probability descending; bytewise ties",
                "weight_interpretation": "subjective judgments, not measured recovery odds",
                "core": {"path": str(core), "sha256": sha(core)},
                "source_identities": {str(p.resolve().relative_to(ROOT)): sha(p) for p in
                    (Path(__file__).resolve(), source_path("swg.py"), Path(automaton.__file__), Path(model.__file__))},
                "files": {name: sha(output / name) for name in
                          ("inputs.json", "branches.json", "source.swg", "model.plan")},
                "source_export": exported, "native_compile": info,
                "seconds": time.monotonic() - began,
                "storage": budget(output, METADATA_CAP, storage_limit)}
    save_new(output / "manifest.json", manifest)  # Last: absence means incomplete preparation.
    budget(output, 0, storage_limit)
    return manifest


def load_bundle(directory, *, core=None):
    directory = Path(directory).resolve()
    manifest = read_json(directory / "manifest.json")
    require(manifest.get("schema") == BUNDLE and set(manifest["files"]) ==
            {"inputs.json", "branches.json", "source.swg", "model.plan"}, "invalid bundle manifest")
    for name, digest in manifest["files"].items():
        require(sha(directory / name) == digest, "bundle changed; compile edited inputs into a new directory")
    recorded = Path(manifest["core"]["path"])
    if core is None:
        core = CORE if CORE.is_file() and sha(CORE) == manifest["core"]["sha256"] else recorded
    core = Path(core)
    require(core.is_file() and sha(core) == manifest["core"]["sha256"],
            "bundle compiler changed or missing; explicit rebuild required")
    # Resolve a relocated identical executable without rewriting immutable metadata.
    manifest = dict(manifest, core=dict(manifest["core"], path=str(core.resolve())))
    return manifest, read_json(directory / "branches.json", METADATA_CAP)


def inspect(core, plan, start, count):
    require(type(count) is int and 0 <= count <= 128, "bounded preview limit")
    if not count:
        return []
    raw = execute([core, "inspect", plan, start, count])
    return [(bytes.fromhex(v), Q(int(p), int(d))) for v, p, d in
            (line.split(" ") for line in raw.decode().splitlines())]


def explain(data, value):
    contributions = []
    for name, graph in data["graphs"].items():
        p = probability(graph, value)
        if p:
            hypothesis = data["hypotheses"][name]
            contribution = Q(hypothesis["normalized_hypothesis_weight"]) * p
            contributions.append({"hypothesis": name, "conditional_probability": str(p),
                                  "contribution": str(contribution), "evidence": hypothesis["evidence"]})
    return contributions


def preview(bundle, *, campaign=None, count=10):
    """Reconsider the NEW model from rank zero against completed evidence only.

    Preparing a preview never dispatches or credits work and never adopts the
    revision. An unacknowledged pending assignment remains eligible.
    """
    require(type(count) is int and 1 <= count <= 50, "preview count must be 1..50")
    directory = Path(bundle).resolve()
    manifest, data = load_bundle(directory)
    core = Path(manifest["core"]["path"])
    full = directory / "model.plan"
    history_count, checked, pending, head, excluded = 0, 0, None, None, 0
    storage = None
    with tempfile.TemporaryDirectory(prefix="eligible-preview-", dir=directory) as temporary:
        active = full
        if campaign is not None:
            with CheckpointCampaign(campaign) as c:
                require(c.context["core"]["sha256"] == manifest["core"]["sha256"],
                        "preview runtime differs from campaign; no implicit upgrade")
                intervals = [(p, a, b-a) for p, ranges in sorted(c.state["ranges"].items()) for a, b in ranges]
                require(len(intervals) <= 512, "history interval limit")
                disk_guard(c.root, 2 * PLAN_CAP)
                budget(directory, PLAN_CAP)
                active = Path(temporary) / "remaining.plan"
                info = json.loads(execute([core, "subtract", full, active,
                    *[x for p, a, n in intervals for x in (c.blob(p), a, n)]], file_cap=PLAN_CAP))
                history_count, checked, pending, head = len(intervals), c.state["checked_negative"], c.state["pending"], c.head
                excluded = int(manifest["native_compile"]["candidates"]) - int(info["candidates"])
                storage = {"campaign_bytes": disk_guard(c.root), "campaign_cap_bytes": CAP,
                           "revision_scratch_reserve_bytes": 2 * PLAN_CAP,
                           "free_disk_reserve_bytes": RESERVE}
        else:
            info = json.loads(execute([core, "describe", full]))
        rows = []
        for rank, (value, p) in enumerate(inspect(core, active, 0, min(count, int(info["candidates"])))):
            contributions = explain(data, value)
            require(sum(Q(x["contribution"]) for x in contributions) == p,
                    "explanation probability disagrees with native plan")
            rows.append({"eligible_rank": rank, "text": value.decode("ascii"), "hex": value.hex(),
                         "probability": str(p), "contributions": contributions})
    return {"ordering": manifest["ordering"], "model_plan_sha256": manifest["files"]["model.plan"],
            "eligible_candidates": int(info["candidates"]), "complete_remaining_support": info["complete_remaining_support"],
            "excluded_completed_strings": excluded, "acknowledged_negatives": checked,
            "history_intervals": history_count, "history_head": head, "pending_not_credited": pending,
            "campaign_unchanged": True, "storage_preflight": storage, "candidates": rows}


def revise(bundle, campaign):
    directory = Path(bundle).resolve()
    manifest, _ = load_bundle(directory)
    with CheckpointCampaign(campaign) as c:
        require(c.context["core"]["sha256"] == manifest["core"]["sha256"],
                "model runtime differs from campaign; no implicit upgrade")
        disk_guard(c.root, 2 * PLAN_CAP)
        result = c.revise(directory / "model.plan")
        return {"model_plan_sha256": manifest["files"]["model.plan"], "revision": result, "status": c.status()}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("compile")
    build.add_argument("inputs", type=Path)
    build.add_argument("new_directory", type=Path)
    build.add_argument("--storage-mib", type=int, default=256)
    view = commands.add_parser("preview")
    view.add_argument("bundle", type=Path)
    view.add_argument("--campaign", type=Path)
    view.add_argument("--count", type=int, default=10)
    adopt = commands.add_parser("revise")
    adopt.add_argument("bundle", type=Path)
    adopt.add_argument("campaign", type=Path)
    args = parser.parse_args()
    if args.command == "compile":
        result = build_bundle(read_json(args.inputs), args.new_directory, storage_limit=args.storage_mib * 1024**2)
    elif args.command == "preview":
        result = preview(args.bundle, campaign=args.campaign, count=args.count)
    else:
        result = revise(args.bundle, args.campaign)
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
