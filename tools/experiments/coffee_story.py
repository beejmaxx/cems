"""User-specified target demonstration, not a blind recovery experiment.

The editable model is tailored to a drink/time-of-day theme. It never receives
the full target, and no complete password is added as a literal candidate.
Explicit byte sets below are a bounded independent TEST oracle, not worker
history. Candidate delivery/checking still uses the existing native pipeline.
"""
from layout import BUILD
import argparse
from collections import defaultdict
from fractions import Fraction as Q
import itertools
import json
from pathlib import Path
import subprocess
import time

from checkpoint_runner import CheckpointCampaign
from model_workflow import CORE, budget, build_bundle, inspect, save_new
from runner import HERE, require, sha

from emitter_v1.model import choice, concat, conditional, let, literal, ref, transform, words


DRINKS = ("cocoa", "coffee", "juice", "latte", "tea", "water")
TIMES = ("afternoon", "dawn", "evening", "morning", "night", "noon")
SEPARATORS = (".", "_", "-", "/")
STYLES = ("lower", "title", "upper")
DESCRIPTIONS = (
    "Initial assumption: two words followed by two digits; lowercase favored.",
    "New ideas: the number may be between the words and may have one digit; separators may differ.",
    "Later fuzzy clue favors title case on both words, one digit, and a reused separator; all earlier alternatives remain.",
)


def policy(stage):
    require(stage in (0, 1, 2), "unknown demonstration stage")
    branches = [("number-at-end-shared", 1 if stage != 1 else 8, False, True)]
    if stage:
        branches += [("middle-independent", 2, True, False),
                     ("middle-shared", 1 if stage == 1 else 8, True, True)]
    styles = (8, 1, 1) if stage < 2 else (1, 8, 1)
    lengths = [(2, 1)] if stage == 0 else ([(1, 1), (2, 9)] if stage == 1 else [(1, 9), (2, 1)])
    return branches, styles, lengths


def inputs(stage):
    """No target argument. Values are alternatives, not a literal full password."""
    def component(name):
        return {"op": "component", "name": name}

    def uniform(values):
        return choice([(str(i), literal(v), 1) for i, v in enumerate(values)])

    branches, style_weights, lengths = policy(stage)
    components = {"drink": uniform(DRINKS), "time": uniform(TIMES), "separator": uniform(SEPARATORS),
                  "number": choice([(str(n), words("0123456789", n), w) for n, w in lengths])}
    style = choice([(name, literal(name), weight) for name, weight in zip(STYLES, style_weights)])

    def styled(name):
        return conditional("style", {s: component(name) if s == "lower" else transform(component(name), s)
                                     for s in STYLES})

    hypotheses = []
    for name, weight, middle, shared in branches:
        separator = ref("s") if shared else component("separator")
        parts = (styled("drink"), separator, component("number"), separator, styled("time")) if middle else (
            styled("drink"), separator, styled("time"), separator, component("number"))
        bindings = [("style", style)] + ([("s", component("separator"))] if shared else [])
        hypotheses.append({"id": name, "weight": weight, "root": let(bindings, concat(*parts)),
            "evidence": {"scope": "invented assumptions for a user-specified synthetic target, not actual recollections",
                         "clue": DESCRIPTIONS[stage]}})
    return {"schema": "search-constructions-v1", "description": DESCRIPTIONS[stage],
            "components": components, "hypotheses": hypotheses}


def oracle(stage):
    """Direct recipe arithmetic independent of emitter graphs/native plans."""
    branches, style_weights, lengths = policy(stage)
    total_branch = sum(b[1] for b in branches)
    total_style, total_length = sum(style_weights), sum(w for _, w in lengths)
    distribution = defaultdict(Q)
    for _, weight, middle, shared in branches:
        separators = [(s, s) for s in SEPARATORS] if shared else list(itertools.product(SEPARATORS, repeat=2))
        for drink, period, (s1, s2), (style, sw), (width, lw) in itertools.product(
                DRINKS, TIMES, separators, zip(STYLES, style_weights), lengths):
            first, second = getattr(drink, style)(), getattr(period, style)()
            probability = Q(weight, total_branch) * Q(sw, total_style) * Q(lw, total_length)
            probability /= len(DRINKS)*len(TIMES)*len(separators)*10**width
            for n in range(10**width):
                number = f"{n:0{width}d}"
                text = first+s1+number+s2+second if middle else first+s1+second+s2+number
                distribution[text.encode("ascii")] += probability
    require(len(distribution) <= 250_000 and sum(distribution.values()) == 1, "bounded oracle mass/support")
    return dict(distribution)


def check_plan(core, plan, expected):
    """Every byte and score, in bounded chunks; never a candidate file."""
    for start in range(0, len(expected), 10000):
        count = min(10000, len(expected)-start)
        r = subprocess.run([str(core), "inspect", str(plan), str(start), str(count)],
                           capture_output=True, check=True, timeout=100)
        require(len(r.stdout) <= 4*1024**2, "oracle inspection size")
        rows = [(bytes.fromhex(v), Q(int(p), int(d))) for v, p, d in
                (line.split(" ") for line in r.stdout.decode().splitlines())]
        require(rows == expected[start:start+count], "native ordering/bytes/scores differ from recipe oracle")


def run(output, *, target_bytes=b"Coffee.8.Morning", core=CORE, checker=None):
    output, core = Path(output).resolve(), Path(core).resolve()
    checker = Path(checker or BUILD / "synthetic-checker").resolve()
    require(not output.exists(), "use a fresh experiment directory")
    budget(output, 160*1024**2)
    output.mkdir(mode=0o700)
    target = output / "FAKE-target.bin"
    with target.open("xb") as f:
        target.chmod(0o600)
        f.write(target_bytes)
    save_new(output / "ground-truth.json", {"scope": "user-specified synthetic target; not blind recovery",
        "password": target_bytes.decode("ascii"), "complete_target_literal_in_model": False})
    began, done, stages, batches = time.monotonic(), set(), [], []
    checked = delivered = 0
    campaign = output / "campaign"
    result = {"schema": "coffee-story-v1", "status": "incomplete", "password": target_bytes.decode("ascii"),
        "scope": "tailored equality-only experiment, not a blind recovery claim",
        "stages": stages, "batches": batches, "real_recovery_coverage_written": False,
        "source_sha256": sha(__file__), "core_sha256": sha(core), "checker_sha256": sha(checker)}
    try:
        for stage in range(3):
            budget(output, 160*1024**2)
            spec, bundle = inputs(stage), output / f"model-{stage}"
            require(target_bytes.decode() not in json.dumps(spec), "complete target leaked into model")
            save_new(output / f"clues-{stage}.json", spec)
            manifest = build_bundle(spec, bundle, core=core)
            distribution = oracle(stage)
            ordered = sorted(distribution.items(), key=lambda row: (-row[1], row[0]))
            remaining = [row for row in ordered if row[0] not in done]
            require(int(manifest["native_compile"]["candidates"]) == len(ordered), "model support mismatch")
            check_plan(core, bundle / "model.plan", ordered)
            rank = next((i for i, (v, _) in enumerate(remaining) if v == target_bytes), None)
            require((rank is None) == (stage == 0), "target support did not match the intended clue stage")
            row = {"stage": stage, "description": DESCRIPTIONS[stage], "model_candidates": len(ordered),
                "eligible_candidates": len(remaining), "completed_overlaps_excluded": len(ordered)-len(remaining),
                "target_eligible_rank_zero_based": rank, "checked_before": len(done),
                "all_model_bytes_and_exact_scores_checked": True, "compile_seconds": manifest["seconds"]}
            stages.append(row)
            if not stage:
                with CheckpointCampaign.create(campaign, bundle / "model.plan", target,
                    [str(checker), "check", "512", "ok"], [str(checker), "confirm"], core=core, checkpoint_every=0):
                    pass
            with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                if stage:
                    row["revision"] = c.revise(bundle / "model.plan")
                require(c.status()["available_in_plan"] == len(remaining), "historical exclusion count mismatch")
                check_plan(core, c.blob(c.state["plan"]), remaining)
                row["all_eligible_bytes_and_exact_scores_checked"] = True
                if stage:
                    require(row["completed_overlaps_excluded"] == len(done), "old negatives lost on revision")
            for attempt in range(2 if stage < 2 else 500):
                with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                    start = c.state["cursor"]
                    submitted = remaining[start:start+768]
                    require(submitted and not (set(v for v, _ in submitted) & done), "acknowledged guess emitted again")
                    answer = c.run_batch(len(submitted))
                    k = answer["committed_negative"]
                    negatives = {v for v, _ in submitted[:k]}
                    require(len(negatives) == k and not (negatives & done) and target_bytes not in negatives,
                            "duplicate or false-negative coverage")
                    done.update(negatives)
                    checked += k + answer["confirmed_hits"]
                    delivered += answer["generated_and_delivered"]
                    require(c.state["checked_negative"] == len(done), "durable history count mismatch")
                    if answer["status"] == "hit":
                        require(submitted[k][0] == target_bytes and c.state["hit"]["hex"] == target_bytes.hex(), "incorrect hit")
                    elif answer["uncredited_tail"]:
                        require(inspect(core, c.blob(c.state["plan"]), c.state["cursor"], 1)[0] == submitted[k],
                                "unchecked tail was lost")
                    batches.append({"stage": stage, **answer})
                    row["last_status"] = c.status()
                # Reopening ensures progress comes from durable state, not a live iterator.
                with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                    require(c.state["cursor"] == start+k and c.state["pending"] is None, "restart mismatch")
                if answer["status"] == "hit":
                    break
            require((row["last_status"]["hit"] is not None) == (stage == 2), "unexpected recovery stage")
            row["checked_after"] = len(done)
            with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                row["checkpoint"] = c.checkpoint()
            save_new(output / f"stage-{stage}.json", row)
            print(json.dumps({"stage": stage, "target_rank": rank, "checked": len(done),
                "completed_overlaps_excluded": row["completed_overlaps_excluded"], "found": stage == 2}), flush=True)
        with CheckpointCampaign(campaign, checkpoint_every=0) as c:
            result["audit"], result["final_status"] = c.audit(), c.status()
            require(result["audit"]["verified"] and c.state["hit"]["hex"] == target_bytes.hex(), "final audit/hit failed")
        require(stages[2]["target_eligible_rank_zero_based"] < stages[1]["target_eligible_rank_zero_based"], "clue did not improve priority")
        result.update(status="passed", actual_equality_checks=checked, delivered_frames=delivered,
            acknowledged_distinct_negatives=len(done), repeated_acknowledged_negatives=0,
            separate_confirmation_checks=1, restarts=len(batches), explicit_byte_sets_are_test_only=True)
    except BaseException as error:
        result["failure"] = repr(error)
        raise
    finally:
        result["seconds"] = time.monotonic()-began
        result["storage_before_results"] = budget(output)
        save_new(output / "results.json", result)
    report = ["# Coffee.8.Morning campaign", "", "Tailored synthetic experiment; not blind recovery or cryptographic cracking.",
        "The user supplied the target. The model contains themed alternatives, not a literal complete password.", "",
        "| Stage | Model size | Target eligible rank (zero-based) | Completed overlaps excluded |",
        "| --- | ---: | ---: | ---: |"]
    for row in stages:
        r = row["target_eligible_rank_zero_based"]
        report.append(f"| {row['stage']}: {row['description']} | {row['model_candidates']:,} | {r if r is not None else 'not admitted'} | {row['completed_overlaps_excluded']:,} |")
    report += ["", f"Recovered **{target_bytes.decode()}** after **{checked:,} actual equality checks**, plus one separate confirmation.",
        f"Zero repeated acknowledged negatives; {delivered:,} frames delivered. Unchecked tails can be delivered again.",
        "Every candidate and exact score in all three full models and all three history-subtracted models matched the independent bounded recipe oracle.",
        "The campaign closed and reopened between batches; its receipt audit passed.",
        "The test oracle uses small explicit byte sets in memory. The unchanged worker stores immutable plans and receipt-backed intervals, not a seen-password table.",
        f"Overall correctness-experiment time: {result['seconds']:.3f} seconds; this includes exhaustive Python oracle work and tiny-batch overhead, not a throughput benchmark.",
        f"Artifacts before results/report: {result['storage_before_results']['retained_bytes']:,} bytes. No candidate wordlists or real recovery coverage were written.", ""]
    with (output / "REPORT.md").open("x") as f:
        (output / "REPORT.md").chmod(0o600)
        f.write("\n".join(report))
    budget(output)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("new_directory", type=Path)
    parser.add_argument("--core", type=Path, default=CORE)
    parser.add_argument("--checker", type=Path, default=BUILD / "synthetic-checker")
    args = parser.parse_args()
    run(args.new_directory, core=args.core, checker=args.checker)
