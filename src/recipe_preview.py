"""Compile and watch private TOML proposals. Never checks passwords or adopts plans."""
import argparse
from fractions import Fraction as Q
import hashlib
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time

from emitter_v1 import automaton, model
import coverage_snapshot as coverage
from model_workflow import BUNDLE, CORE, METADATA_CAP, budget, expand, explain, inspect, read_json, save_new
from plan_inspect import choose, quoted, rank_number
from recipe import compile_recipe, read_recipe, require
from runner import PLAN_CAP, execute, sha
from swg import write_source
from workspace import Workspace


def root_key(root):
    return hashlib.sha256(model.canonical(root).encode()).hexdigest()


def cached_graphs(directory):
    directory = Path(directory)
    inputs = expand(read_json(directory / "inputs.json"))
    data = read_json(directory / "branches.json", METADATA_CAP)
    if "graphs" in data and "hypotheses" in data:
        manifest = read_json(directory / "manifest.json")
        require(manifest.get("schema") == BUNDLE, "invalid cached bundle")
        require(manifest["core"]["sha256"] == sha(CORE), "cache uses a different native engine")
        for name, digest in manifest["files"].items():
            require(sha(directory / name) == digest, "cache differs from its manifest")
        identities = manifest.get("source_identities", {})
        for key, module_path in (("src/emitter_v1/automaton.py", automaton.__file__), ("src/emitter_v1/model.py", model.__file__)):
            require(identities.get(key) == sha(module_path), "cached graph compiler differs; explicit rebuild required")
        return {root_key(b["root"]): data["graphs"][b["id"]] for b in inputs}
    result = {}
    collection = directory.parent
    manifest = read_json(collection / "manifest.json", METADATA_CAP)
    require(manifest["core_sha256"] == sha(CORE), "cache native engine differs")
    for suffix, module_path in (("/src/emitter_v1/automaton.py", automaton.__file__), ("/src/emitter_v1/model.py", model.__file__)):
        matches = [v for k, v in manifest["sources"].items() if k.endswith(suffix)]
        require(matches == [sha(module_path)], "cached graph compiler differs; explicit rebuild required")
    for branch in inputs:
        key = root_key(branch["root"])
        row = data[branch["id"]]
        path = (collection / row["graph_file"]).resolve()
        require(path.is_relative_to(collection.resolve()) and path.name == key + ".json", "graph cache source identity differs")
        require(sha(path) == row["graph_sha256"], "cached graph changed")
        result[key] = read_json(path, METADATA_CAP)
    return result


def recipe_source(recipe):
    source = recipe.get("source")
    if source is None:
        return None
    require(set(source) == {"model", "inputs_sha256", "plan_sha256", "core_sha256"}, "source must pin model, inputs, plan and engine")
    entry, plan = choose(source["model"], full=True)
    require(sha(plan.path) == source["plan_sha256"] and (not plan.digest or plan.digest == source["plan_sha256"]), "source plan changed")
    require(sha(CORE) == source["core_sha256"], "source engine changed")
    require(sha(plan.path.parent / "inputs.json") == source["inputs_sha256"], "source construction inputs changed")
    return plan.path.parent


def build_proposal(recipe, raw, destination, *, reuse=None):
    spec = compile_recipe(recipe)
    branches = expand(spec)
    started = time.monotonic()
    source = recipe_source(recipe)
    cache = cached_graphs(source) if source else {}
    if reuse:
        cache.update(cached_graphs(reuse))
    require(not destination.exists(), "preview output already exists")
    budget(destination, 2 * PLAN_CAP + METADATA_CAP)
    destination.mkdir(mode=0o700)
    save_new(destination / "inputs.json", spec)
    graphs, weights, details = {}, {}, {}
    reused = 0
    for i, branch in enumerate(branches, 1):
        key = root_key(branch["root"])
        if key in cache:
            reused += 1
        else:
            print(f"Compiling {i}/{len(branches)}: {branch['id']}", flush=True)
            cache[key] = automaton.compile_probability(model.model(branch["root"])).payload()
        name = branch["id"]
        graphs[name], weights[name] = cache[key], Q(branch["weight"])
        details[name] = {"weight": branch["weight"], "evidence": branch["evidence"]}
    total = sum(weights.values())
    for name in details:
        details[name]["normalized_hypothesis_weight"] = str(weights[name] / total)
    save_new(destination / "branches.json", {"graphs": graphs, "hypotheses": details})
    exported = write_source(destination / "source.swg", graphs, weights)
    print(f"Ranking exact mixture ({reused}/{len(branches)} graphs reused)…", flush=True)
    info = json.loads(execute([CORE, "compile", destination / "source.swg", destination / "model.plan"], file_cap=PLAN_CAP))
    require(info["complete_remaining_support"], "preview compilation returned incomplete support")
    os.chmod(destination / "model.plan", 0o600)
    review = {"status": "proposal", "adopted": False, "real_password_checks": 0,
              "recipe_sha256": hashlib.sha256(raw).hexdigest(), "reused_graphs": reused}
    if recipe.get("history"):
        h = recipe["history"]
        require(set(h) == {"resource", "policy", "coverage_sha256"}, "history must pin resource, policy and coverage metadata")
        history = Workspace().path(h["resource"])
        require(sha(history / "coverage.json") == h["coverage_sha256"], "history snapshot changed; review the recipe's history setting")
        snapshot = coverage.load(history, policy=h["policy"], core=CORE)
        print("Subtracting the recipe's pinned accepted history…", flush=True)
        remaining = json.loads(execute([CORE, "subtract", destination / "model.plan", destination / "remaining.plan",
                                      history / "completed.plan", 0, snapshot["completed"]["count"]], file_cap=PLAN_CAP))
        os.chmod(destination / "remaining.plan", 0o600)
        review["remaining"] = {"sha256": sha(destination / "remaining.plan"), "count": int(remaining["candidates"]),
                               "history_policy": h["policy"], "coverage_sha256": h["coverage_sha256"]}
    (destination / "recipe.toml").write_bytes(raw); (destination / "recipe.toml").chmod(0o600)
    manifest = {"schema": BUNDLE, "input_schema": spec["schema"],
                "ordering": "exact summed string probability descending; bytewise ties",
                "weight_interpretation": "unadopted subjective experiments, not measured recovery odds",
                "core": {"path": str(CORE.resolve()), "sha256": sha(CORE)},
                "source_identities": {"src/emitter_v1/automaton.py": sha(automaton.__file__),
                                      "src/emitter_v1/model.py": sha(model.__file__),
                                      "src/recipe.py": sha(Path(__file__).with_name("recipe.py")),
                                      "src/recipe_preview.py": sha(__file__)},
                "files": {name: sha(destination / name) for name in ("inputs.json", "branches.json", "source.swg", "model.plan")},
                "source_export": exported, "native_compile": info, "review": review,
                "seconds": time.monotonic() - started, "storage": budget(destination)}
    save_new(destination / "manifest.json", manifest)
    print(f"Prepared {int(info['candidates']):,} full-model candidates in {manifest['seconds']:.1f}s.", flush=True)
    return manifest


def print_preview(directory, ranks, count, *, full=False, previous=None):
    manifest = read_json(directory / "manifest.json")
    require(sha(CORE) == manifest["core"]["sha256"], "preview engine differs from its manifest")
    remaining = manifest.get("review", {}).get("remaining")
    view = "full" if full or not remaining else "remaining"
    filename = "model.plan" if view == "full" else "remaining.plan"
    plan = directory / filename
    digest = manifest["files"][filename] if view == "full" else remaining["sha256"]
    require(sha(plan) == digest, "preview plan differs from its manifest")
    total = int(manifest["native_compile"]["candidates"]) if view == "full" else remaining["count"]
    data = read_json(directory / "branches.json", METADATA_CAP)
    print(f"\nPROPOSAL {directory.name} | {view} | {total:,} candidates", flush=True)
    print("Exact model weights; not measured recovery odds. No password checks or adoption.")
    if view == "remaining":
        print("Exclusions: " + remaining["history_policy"])
    before = None
    if previous and (Path(previous) / filename).is_file():
        before = Path(previous) / filename
        old = json.loads(execute([CORE, "describe", before]))
        old_count = int(old["candidates"])
    report = {"view": view, "plan_sha256": digest, "candidates": total, "windows": []}
    for rank in ranks:
        print(f"\nRank {rank:,}:")
        if rank > total:
            print("  Outside this model's remaining range." if view == "remaining" else "  Outside this model's range.")
            report["windows"].append({"rank": rank, "outside_range": True})
            continue
        rows = []
        for offset, (value, score) in enumerate(inspect(CORE, plan, rank - 1, min(count, total - rank + 1))):
            parts = explain(data, value)
            require(sum(Q(p["contribution"]) for p in parts) == score, "candidate score disagrees with recipe contributions")
            lead = max(parts, key=lambda p: Q(p["contribution"]))["hypothesis"]
            print(f"  {rank+offset:>19,}  {quoted(value)}")
            print(f"                       main explanation: {lead}")
            rows.append({"rank": rank+offset, "hex": value.hex(), "score": str(score), "contributions": parts})
        if before and rank <= old_count:
            prior = inspect(CORE, before, rank - 1, min(count, old_count-rank+1))
            if [x[0] for x in prior] != [bytes.fromhex(x["hex"]) for x in rows]:
                print("  Previously at these ranks: " + " | ".join(quoted(v) for v, _ in prior))
        report["windows"].append({"rank": rank, "rows": rows})
    print("\nSaved proposal: " + str(directory), flush=True)
    return report


def terminate(proc):
    if proc.poll() is None:
        try:
            os.killpg(proc.pid, signal.SIGTERM)
        except ProcessLookupError:
            proc.wait(); return
        try:
            proc.wait(timeout=2)
        except subprocess.TimeoutExpired:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            proc.wait()


def watch(path, output, ranks, count, *, once=False, full=False):
    require(1 <= count <= 20 and 1 <= len(ranks) <= 50, "use count 1..20 and at most 50 ranks")
    path = path.expanduser().resolve()
    require(path.is_file(), "recipe file does not exist")
    output.mkdir(mode=0o700, parents=True, exist_ok=True)
    latest, attempted = None, None
    print("Recipe: " + str(path), flush=True)
    print("Preview only. Save the TOML file to rebuild; Ctrl-C stops the watcher.", flush=True)
    while True:
        try:
            raw = path.read_bytes()
        except OSError as error:
            if once:
                raise
            print(f"Waiting for recipe: {error}", flush=True); time.sleep(1); continue
        digest = hashlib.sha256(raw).hexdigest()
        if digest == attempted:
            if once:
                return
            time.sleep(0.25); continue
        if not once:
            time.sleep(0.35)
            if not path.exists() or path.read_bytes() != raw:
                continue
        attempted = digest
        try:
            recipe = read_recipe(path)
            compile_recipe(recipe)  # Validate before starting an expensive preparation.
            require(path.read_bytes() == raw, "recipe changed while reading; save again")
        except (ValueError, KeyError, TypeError, OSError) as error:
            print(f"Recipe error: {error}\nLast successful preview is unchanged.", flush=True)
            if once:
                raise ValueError(str(error)) from None
            continue
        # Include implementation and engine identities: code changes never reuse an
        # old result merely because the TOML text is unchanged.
        identity = hashlib.sha256(raw + b"".join(Path(p).read_bytes() for p in
            (__file__, Path(__file__).with_name("recipe.py"), automaton.__file__, model.__file__,
             Path(__file__).with_name("model_workflow.py"), Path(__file__).with_name("swg.py"), CORE))).hexdigest()
        destination = output / ("recipe-" + identity[:16])
        print(f"\nPreparing saved recipe {digest[:12]}…", flush=True)
        if latest:
            print("Previous samples describe the preceding successful draft until this preparation completes.", flush=True)
        changed = False
        if not destination.exists():
            with tempfile.TemporaryDirectory(prefix=".recipe-building-", dir=output) as tmp:
                work = Path(tmp)
                job = {"recipe": recipe, "raw": raw.decode(), "output": str(work / "bundle"),
                       "reuse": str(latest) if latest else None}
                save_new(work / "job.json", job)
                proc = subprocess.Popen([sys.executable, "-B", str(Path(__file__).resolve()), "--build-job", str(work / "job.json")],
                                        start_new_session=True)
                try:
                    while proc.poll() is None:
                        time.sleep(0.2)
                        if not path.exists() or path.read_bytes() != raw:
                            changed = True
                            print("Recipe changed; discarding unfinished preview and rebuilding the latest save.", flush=True)
                            terminate(proc); break
                    if changed:
                        continue
                    if proc.returncode != 0:
                        print("Preparation failed. Last successful preview is unchanged.", flush=True)
                        if once:
                            raise RuntimeError("recipe preparation failed")
                        continue
                    if not path.exists() or path.read_bytes() != raw:
                        print("Recipe changed after preparation; rebuilding the latest save.", flush=True)
                        continue
                    (work / "bundle").rename(destination)
                finally:
                    terminate(proc)
        # A cached output is usable only for the same recipe and pinned sources.
        try:
            recipe_source(recipe)
            manifest = read_json(destination / "manifest.json")
            require(manifest["review"]["recipe_sha256"] == digest, "cached preview recipe differs")
            if recipe.get("history"):
                require(sha(Workspace().path(recipe["history"]["resource"]) / "coverage.json") == recipe["history"]["coverage_sha256"], "history snapshot changed")
            report = print_preview(destination, ranks, count, full=full, previous=latest)
            report_path = destination / "preview.json"
            if not report_path.exists():
                save_new(report_path, report)
            latest = destination
        except (ValueError, RuntimeError, OSError, KeyError) as error:
            print(f"Preview error: {error}\nLast successful preview is unchanged.", flush=True)
            if once:
                raise
        if once:
            return latest


def main():
    if sys.argv[1:2] == ["--build-job"]:
        job = read_json(sys.argv[2], METADATA_CAP)
        build_proposal(job["recipe"], job["raw"].encode(), Path(job["output"]), reuse=job["reuse"])
        return
    parser = argparse.ArgumentParser(prog="cems watch", description=__doc__)
    parser.add_argument("recipe", type=Path, nargs="?", help="editable TOML recipe; defaults to workspace models/RECIPE.toml")
    parser.add_argument("--at", nargs="+", type=rank_number, default=[1, 10**6, 10**9, 10**11, 10**12, 10**14])
    parser.add_argument("--count", type=int, default=5, help="candidates per rank, 1..20")
    parser.add_argument("--once", action="store_true", help="prepare one preview and exit")
    parser.add_argument("--full", action="store_true", help="show ranks before history exclusions")
    parser.add_argument("--output", type=Path, help="preview collection; defaults to workspace prepared-models")
    args = parser.parse_args()
    output = args.output or Workspace().resolve("prepared-models")
    path = args.recipe or Workspace().resolve("models/RECIPE.toml")
    watch(path, output, list(dict.fromkeys(args.at)), args.count, once=args.once, full=args.full)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nWatcher stopped; proposals remain saved."); raise SystemExit(130)
    except (ValueError, RuntimeError, OSError, KeyError, TypeError) as error:
        raise SystemExit("cems watch: " + str(error)) from None
