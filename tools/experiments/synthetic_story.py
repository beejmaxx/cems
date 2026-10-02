"""Reproducible, target-independent fuzzy-recollection campaign; equality ONLY.

The secret factory, fixed clue schedule, and exhaustive test oracle are separate.
No search decision reads the secret. Explicit sets exist only in this bounded
test oracle, never in the worker's persistent history or production hot path.
"""
from layout import BUILD, source as source_path
from collections import defaultdict
from fractions import Fraction as Q
import itertools
import json
from pathlib import Path
import random
import time

from checkpoint_runner import CheckpointCampaign
from model_workflow import CORE, budget, build_bundle, inspect, preview, save_new
from runner import HERE, require, sha

from emitter_v1.model import choice, concat, let, literal, ref, transform, words


def component(name):
    return {"op": "component", "name": name}


def alternatives(values):
    return choice([(f"alternative-{i}", literal(value), 1) for i, value in enumerate(values)])


def scenario_inputs(stage):
    """No seed/secret argument: all three clue schedules are frozen in advance.

    Later clues identify a construction class, NOT the complete target or its
    word/number values. The revised domain still contains thousands of choices.
    """
    require(stage in (0, 1, 2), "unknown clue stage")
    components = {
        "prefix": alternatives(["!", "~"]),
        "tree": alternatives(["cedar", "maple", "birch"]),
        "first": choice([
            ("lowercase", component("tree"), 9 if stage < 2 else 1),
            ("title-case", transform(component("tree"), "title"), 1 if stage < 2 else 9),
        ]),
        "separator": alternatives(["#", "-"]),
        "number": words("0123456789", 2),
        "second": alternatives(["orbit", "vector"]),
        "ending": alternatives(["?", "!!"]),
    }

    def shared(middle_number):
        middle = [component("number"), ref("s"), component("second")] if middle_number else [
            component("second"), ref("s"), component("number")]
        return let([("s", component("separator"))],
                   concat(component("prefix"), component("first"), ref("s"), *middle, component("ending")))

    hypotheses = [{"id": "number-at-end", "weight": [1, 100, 1][stage], "root": shared(False),
                   "evidence": {"clue": "Initial recollection: two words, then two digits.",
                                "certainty": "subjective; may be mistaken"}}]
    if stage:
        hypotheses.extend([
            {"id": "number-in-middle-independent-separators", "weight": 20 if stage == 1 else 2,
             "root": concat(component("prefix"), component("first"), component("separator"),
                            component("number"), component("separator"), component("second"), component("ending")),
             "evidence": {"clue": "New possibility: digits between the words; separators might differ."}},
            {"id": "number-in-middle-shared-separator", "weight": 1 if stage == 1 else 12,
             "root": shared(True),
             "evidence": {"clue": "One separator choice, reused twice; plausible alternative construction."}},
        ])
    return {"schema": "search-constructions-v1", "description": [
        "Imperfect initial recollection. The true construction is not admitted yet.",
        "Add two overlapping middle-number constructions, initially given little weight.",
        "Revised recollection favors title case and a separator chosen once and reused."
    ][stage], "components": components, "hypotheses": hypotheses}


def make_secret(seed):
    """Independent artificial ground-truth recipe; not a secure password generator."""
    rng = random.Random(seed)
    recipe = {"prefix": rng.choice(["!", "~"]), "first": rng.choice(["cedar", "maple", "birch"]).title(),
              "separator": rng.choice(["#", "-"]), "number": f"{rng.randrange(100):02d}",
              "second": rng.choice(["orbit", "vector"]), "ending": rng.choice(["?", "!!"])}
    value = (recipe["prefix"] + recipe["first"] + recipe["separator"] + recipe["number"] +
             recipe["separator"] + recipe["second"] + recipe["ending"]).encode("ascii")
    return value, recipe


def exhaustive_oracle(stage):
    """Hand-written construction arithmetic, independent of emitter/DAG compilation.

    Deliberately bounded: 28,800 distinct strings at most. This is not the worker
    implementation and its explicit dictionary is never persisted as coverage.
    """
    branch_weights = {"old": 1} if stage == 0 else (
        {"old": 100, "independent": 20, "shared": 1} if stage == 1 else
        {"old": 1, "independent": 2, "shared": 12})
    case_weights = {"lower": Q(9, 10), "title": Q(1, 10)} if stage < 2 else {
        "lower": Q(1, 10), "title": Q(9, 10)}
    result = defaultdict(Q)
    for branch, weight in branch_weights.items():
        separators = list(itertools.product("#-", repeat=2)) if branch == "independent" else [(s, s) for s in "#-"]
        for prefix, word, case, (s1, s2), number, second, ending in itertools.product(
                ["!", "~"], ["cedar", "maple", "birch"], ["lower", "title"], separators,
                range(100), ["orbit", "vector"], ["?", "!!"]):
            first = word.title() if case == "title" else word
            tail = f"{second}{s2}{number:02d}" if branch == "old" else f"{number:02d}{s2}{second}"
            value = (prefix + first + s1 + tail + ending).encode("ascii")
            p = Q(weight, sum(branch_weights.values())) * case_weights[case] / (2 * 3 * len(separators) * 100 * 2 * 2)
            result[value] += p
    require(sum(result.values()) == 1, "test oracle mass is not one")
    return dict(result)


def ordered(distribution, done=()):
    return sorted(((v, p) for v, p in distribution.items() if v not in done), key=lambda row: (-row[1], row[0]))


def check_entire_plan(core, plan, expected):
    """Exhaustively compare bytes AND exact probability, not just selected samples."""
    for start in range(0, len(expected), 128):
        actual = inspect(core, plan, start, min(128, len(expected) - start))
        require(actual == expected[start:start + 128], "native ordering/scores differ from independent recipe oracle")


def rank_of(rows, value):
    return next((i for i, (v, _) in enumerate(rows) if v == value), None)


def run_story(output, *, seed=20260930, core=CORE, checker=None):
    output = Path(output).resolve()
    require(not output.exists(), "simulation output exists; choose a new directory")
    budget(output, 160 * 1024**2)
    output.mkdir(mode=0o700)
    began = time.monotonic()
    core = Path(core).resolve()
    checker = str(Path(checker or BUILD / "synthetic-checker").resolve())
    secret, recipe = make_secret(seed)
    target = output / "FAKE-target.bin"
    with target.open("xb") as stream:
        target.chmod(0o600)
        stream.write(secret)
    # The search inputs do not receive secret/recipe/seed. They can be inspected
    # before execution, and exactly the same inputs work for the whole seed cohort.
    save_new(output / "ground-truth.json", {"scope": "FAKE recipe, not private recovery data",
        "seed": seed, "recipe": recipe, "password": secret.decode(), "target_sha256": sha(target)})
    campaign = output / "campaign"
    done, batches, stages = set(), [], []
    delivered_total, checked_total, overlap_witnesses = 0, 0, []
    result = {"schema": "fuzzy-story-result-v1", "status": "incomplete", "seed": seed,
        "checker": "native byte-equality, no cryptographic work", "real_recovery_coverage_written": False,
        "ordering": "exact summed string probability; bytewise ties",
        "target_independent_model_builder": True, "password": secret.decode(), "stages": stages, "batches": batches}

    for stage in range(3):
        spec = scenario_inputs(stage)
        save_new(output / f"clues-{stage}.json", spec)
        bundle = output / f"model-{stage}"
        manifest = build_bundle(spec, bundle, core=core)
        distribution = exhaustive_oracle(stage)
        full_order = ordered(distribution)
        require(int(manifest["native_compile"]["candidates"]) == len(distribution), "oracle support size mismatch")
        check_entire_plan(core, bundle / "model.plan", full_order)
        step = {"stage": stage, "description": spec["description"], "model_support": len(distribution),
                "target_full_model_rank": rank_of(full_order, secret), "compile_seconds": manifest["seconds"],
                "compile_native_peak_rss_bytes": manifest["native_compile"].get("peak_rss_bytes"),
                "checked_before": len(done), "full_model_exhaustively_verified": True}
        stages.append(step)
        if not stage:
            require(secret not in distribution, "initial misconception must genuinely exclude the secret")
            with CheckpointCampaign.create(campaign, bundle / "model.plan", target,
                    [checker, "check", "128", "ok"], [checker, "confirm"], core=core, checkpoint_every=0):
                pass
        before_preview = time.monotonic()
        step["preview"] = preview(bundle, campaign=campaign, count=6)
        step["preview_seconds"] = time.monotonic() - before_preview
        remaining = ordered(distribution, done)
        step["target_remaining_rank"] = rank_of(remaining, secret)
        step["eligible_before"] = len(remaining)
        step["completed_overlaps_excluded"] = len(distribution.keys() & done)
        if stage:
            require(step["completed_overlaps_excluded"] > 0, "revision did not exercise overlapping history")
            overlap_witnesses.extend(v.decode() for v in sorted(distribution.keys() & done)[:2])
        with CheckpointCampaign(campaign, checkpoint_every=0) as c:
            if stage:
                step["replan"] = c.revise(bundle / "model.plan")
            active = c.blob(c.state["plan"])
            require(c.status()["available_in_plan"] == len(remaining), "remaining support differs from byte-set subtraction")
            check_entire_plan(core, active, remaining)
            step["eligible_order_and_exclusion_exhaustively_verified"] = True
            require([x["hex"] for x in step["preview"]["candidates"]] == [v.hex() for v, _ in remaining[:6]],
                    "preview does not describe actual adopted plan")

        # Close/reopen between EVERY batch: the next call relies on durable
        # history, not an in-memory generator cursor or set of prior passwords.
        max_batches = 2 if stage < 2 else 100
        step["batches"] = 0
        for _ in range(max_batches):
            with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                start = c.state["cursor"]
                submitted = remaining[start:start + 256]
                require(submitted and not (set(v for v, _ in submitted) & done), "acknowledged candidate dispatched again")
                answer = c.run_batch(256)
                k = answer["committed_negative"]
                evaluated = [v for v, _ in submitted[:k]]
                require(secret not in evaluated, "a correct candidate was recorded as negative")
                require(len(evaluated) == len(set(evaluated)) and not (set(evaluated) & done), "duplicate negative check")
                done.update(evaluated)
                delivered_total += answer["generated_and_delivered"]
                checked_total += k + answer["confirmed_hits"]
                if answer["status"] == "hit":
                    require(submitted[k][0] == secret and c.state["hit"]["hex"] == secret.hex(), "incorrect recovered bytes")
                elif answer["uncredited_tail"]:
                    require(inspect(core, c.blob(c.state["plan"]), c.state["cursor"], 1)[0] == submitted[k],
                            "delivered but unchecked tail was skipped")
                require(c.state["checked_negative"] == len(done), "durable count differs from explicit test oracle")
                batches.append({"stage": stage, **answer})
                step["batches"] += 1
                if step["batches"] == 1 or answer["status"] == "hit":
                    c.checkpoint()
                step["last_status"] = c.status()
                if answer["status"] == "hit":
                    break
        require((step["last_status"]["hit"] is not None) == (stage == 2), "unexpected recovery stage")
        step["checked_after"] = len(done)
        print(json.dumps({"stage": stage, "target_rank": step["target_remaining_rank"],
            "completed_excluded": step["completed_overlaps_excluded"], "checked": len(done),
            "found": step["last_status"]["hit"] is not None}), flush=True)

    with CheckpointCampaign(campaign, checkpoint_every=0) as c:
        c.checkpoint()
        result["audit"] = c.audit()
        result["final_status"] = c.status()
        require(c.state["hit"]["hex"] == secret.hex() and result["audit"]["verified"], "reopened hit/audit failed")
    require(stages[2]["target_remaining_rank"] < stages[1]["target_remaining_rank"], "new weights did not promote this recipe")

    # Negative control: exhausting the original unsupported model cannot find a
    # secret outside it, and depletion must not be reported as recovery.
    with CheckpointCampaign.create(output / "negative-control", output / "model-0/model.plan", target,
            [checker, "check", "all", "ok"], [checker, "confirm"], core=core, checkpoint_every=0) as c:
        control = c.run_batch(100_000)
        require(control["status"] == "negative" and c.run_batch(1)["status"] == "plan_depleted" and
                c.state["hit"] is None, "negative control falsely found a password")
        c.checkpoint()
        result["negative_control"] = {"checked": control["committed_negative"], "found": False,
                                      "meaning": "initial model exhausted, not all possible passwords exhausted",
                                      "audit": c.audit()}

    result.update(status="passed", acknowledged_distinct_negatives=len(done), acknowledged_duplicate_checks=0,
        actual_equality_evaluations_main_campaign=checked_total, delivered_frames_main_campaign=delivered_total,
        separately_confirmed_hits=1, completed_overlap_examples=overlap_witnesses,
        partial_tails_remained_eligible=True, restarts_between_batches=len(batches),
        explicit_set_is_test_oracle_only=True, seconds=time.monotonic() - began,
        storage=budget(output), identities={"core": sha(core), "checker": sha(checker),
        "scenario_source": sha(__file__), "workflow_source": sha(source_path("model_workflow.py"))})
    save_new(output / "results.json", result)
    lines = ["# Generated-password campaign", "", "Synthetic byte-equality experiment only. No real recovery history was modified.",
             "", f"Generated password: `{secret.decode()}` (seed {seed}).", "",
             "The search-input builder never receives the password or seed. Fixed revisions change the construction domains and subjective weights.",
             "", "| Stage | Admitted strings | Target's eligible rank (zero-based) | Completed overlaps removed | Negatives after stage |",
             "| --- | ---: | ---: | ---: | ---: |"]
    for s in stages:
        lines.append(f"| {s['stage']}: {s['description']} | {s['model_support']:,} | {s['target_remaining_rank']} | "
                     f"{s['completed_overlaps_excluded']:,} | {s['checked_after']:,} |")
    lines += ["", f"Recovered and confirmed after {checked_total:,} actual equality evaluations in the main campaign.",
              f"Zero acknowledged negatives were checked again. {delivered_total:,} frames were delivered; deliberately untested tails could be delivered again.",
              "Every model and revised eligible sequence was exhaustively compared with an independent Cartesian-product/Fraction oracle, including exact probabilities.",
              f"Reopened between {len(batches)} batches; final checkpoint and receipt audit passed.",
              f"Negative control checked all {result['negative_control']['checked']:,} initial-model candidates and correctly found nothing.",
              "", f"Elapsed: {result['seconds']:.3f} s, including exhaustive test-oracle work. Retained artifact bytes before this report: {result['storage']['retained_bytes']:,}.",
              "", "This engineered scenario validates the workflow, not the chance of recovering a real password. Later clues deliberately describe the true construction class; the system does not invent useful memories.",
              "The worker stores immutable plans and completed intervals, not the exhaustive test oracle. Lost acknowledgments may cause safe repeat checks; this run exercises acknowledged partial work and clean process restarts.", ""]
    with (output / "REPORT.md").open("x") as stream:
        (output / "REPORT.md").chmod(0o600)
        stream.write("\n".join(lines))
    budget(output)
    return result


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("new_directory", type=Path)
    parser.add_argument("--seed", type=int, default=20260930)
    args = parser.parse_args()
    answer = run_story(args.new_directory, seed=args.seed)
    print(json.dumps({"status": answer["status"], "password": answer["password"],
                      "checks": answer["actual_equality_evaluations_main_campaign"],
                      "storage_bytes": budget(args.new_directory)["retained_bytes"],
                      "seconds": answer["seconds"]}, indent=2))
