"""Five fuzzy revisions and tens/hundreds of millions of ACTUAL equality checks.

Every delivered frame is also compared with independent recipe arithmetic in a
separate native audit pass. That pass never opens the password target and is
not counted as password checking. No raw candidate list is retained.
"""
from layout import BUILD, source as source_path
import argparse
import copy
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time

from checkpoint_runner import CheckpointCampaign
from large_story_model import Configuration, DESCRIPTIONS, RecipeModel, RecipeIndex, RecipeHistory, inputs, secret
from model_workflow import CORE, budget, build_bundle, explain, inspect, load_bundle, save_new
from runner import HERE, child_limits, read_small, require, sha


def audit_stream(core, auditor, plan, oracle, start, count):
    began = time.monotonic()
    producer = consumer = None
    with tempfile.TemporaryFile() as pe, tempfile.TemporaryFile() as ce:
        try:
            producer = subprocess.Popen([str(core), "emit", str(plan), str(start), str(count)],
                stdout=subprocess.PIPE, stderr=pe, preexec_fn=child_limits)
            consumer = subprocess.Popen([str(auditor), str(oracle), str(start), str(count)],
                stdin=producer.stdout, stdout=subprocess.PIPE, stderr=ce, preexec_fn=child_limits)
            producer.stdout.close()
            output, _ = consumer.communicate(timeout=100)
            producer.wait(timeout=max(0.1, 100-(time.monotonic()-began)))
            require(producer.returncode == 0 and consumer.returncode == 0,
                    "independent stream audit failed: " + read_small(pe).decode(errors="replace") + read_small(ce).decode(errors="replace"))
            require(len(output) <= 65536, "audit result size")
            result = json.loads(output)
            require(result["verified"] is True and result["frames_compared_exactly"] == count, "audit count")
            result["wall_seconds"] = time.monotonic()-began
            return result
        finally:
            for process in (consumer, producer):
                if process is not None and process.poll() is None:
                    process.kill()
                    process.wait()
            if producer and producer.stdout:
                producer.stdout.close()


def check_windows(core, plan, index):
    offsets = sorted({0, max(0, index.count-16), *[n for n in (10**6, 10**9, 10**12) if n+16 <= index.count]})
    points = 0
    for offset in offsets:
        count = min(16, index.count-offset)
        expected = [index.at(offset+i) for i in range(count)]
        require(inspect(core, plan, offset, count) == expected, "native bytes/scores differ from independent recipe index")
        for i, (value, score) in enumerate(expected):
            require(index.rank(value) == offset+i and index.model.score(value) == score, "oracle rank round trip")
        points += count
    return {"offsets": offsets, "points": points}


def history_contains(history, model, value):
    parsed = model.parse(value)
    if parsed is None:
        return False
    key, n = parsed
    return any(a <= n < b for a, b in history.ranges.get(key, ()))


def run(output, *, seed=20261001, unit=10_000_000, cfg=Configuration(), core=CORE, auditor=None):
    require(1_000_000 <= unit <= 20_000_000, "actual-check unit must be 1M..20M")
    output, core = Path(output).resolve(), Path(core).resolve()
    auditor = Path(auditor or BUILD / "recipe-audit").resolve()
    checker = BUILD / "synthetic-checker"
    require(not output.exists(), "experiment exists; use a fresh directory")
    budget(output, 160*1024**2)
    output.mkdir(mode=0o700)
    began = time.monotonic()
    password, recipe = secret(seed, cfg)
    target = output / "FAKE-target.bin"
    with target.open("xb") as f:
        target.chmod(0o600)
        f.write(password)
    save_new(output / "ground-truth.json", {"scope": "artificial test only", "seed": seed,
        "password": password.decode(), "recipe": recipe, "model_builder_receives_no_password_or_seed": True})
    campaign = output / "campaign"
    history, probes = RecipeHistory(), []
    batches, stages = [], []
    result = {"schema": "large-fuzzy-story-v1", "status": "incomplete", "scope": "actual CPU equality; no cryptography",
        "password": password.decode(), "seed": seed, "unit": unit, "stages": stages, "batches": batches,
        "real_recovery_data_modified": False, "synthetic_receipts_fabricated": 0,
        "source_identities": {name: sha(source_path(name)) for name in
            ("large_story.py", "large_story_model.py", "recipe_audit.cpp", "model_workflow.py", "runner.py", "checkpoint_runner.py")},
        "binaries": {"core": sha(core), "checker": sha(checker), "stream_auditor": sha(auditor)}}
    checked = delivered = audited = 0
    try:
        for stage in range(5):
            budget(output, 160*1024**2)
            spec, bundle = inputs(stage, cfg), output / f"model-{stage}"
            save_new(output / f"clues-{stage}.json", spec)
            manifest = build_bundle(spec, bundle, core=core)
            full_model = RecipeModel(stage, cfg)
            full_index, eligible = RecipeIndex(full_model), RecipeIndex(full_model, history)
            require(int(manifest["native_compile"]["candidates"]) == full_index.count, "full model count mismatch")
            oracle_path = output / f"oracle-{stage}.recipes"
            oracle_stats = eligible.export(oracle_path)
            row = {"stage": stage, "description": DESCRIPTIONS[stage], "full_candidates": full_index.count,
                "derivation_occurrences": full_model.derivations, "eligible_candidates": eligible.count,
                "completed_overlaps_excluded": full_index.count-eligible.count,
                "target_full_rank": full_index.rank(password), "target_eligible_rank": eligible.rank(password),
                "checked_before": history.count, "compile_seconds": manifest["seconds"], "compile": manifest["native_compile"],
                "independent_oracle": oracle_stats, "batches": 0}
            stages.append(row)
            if stage in (0, 2):
                require(row["target_full_rank"] is None, "wrong-model control accidentally admits target")
            else:
                require(row["target_eligible_rank"] is not None, "reintroduced target was incorrectly excluded")
            row["full_plan_windows"] = check_windows(core, bundle / "model.plan", full_index)
            if stage == 0:
                with CheckpointCampaign.create(campaign, bundle / "model.plan", target,
                    [str(checker), "check", str(unit), "ok"], [str(checker), "confirm"], core=core, checkpoint_every=0):
                    pass
            with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                if stage:
                    row["revision"] = c.revise(bundle / "model.plan")
                require(c.status()["available_in_plan"] == eligible.count, "native history subtraction count mismatch")
                row["eligible_plan_windows"] = check_windows(core, c.blob(c.state["plan"]), eligible)
                # Samples from past dispatch boundaries test inclusion as well as
                # exclusion, including withdrawn/reintroduced construction sets.
                for value in probes:
                    expected = bool(full_model.score(value)) and not history_contains(history, full_model, value)
                    require((eligible.rank(value) is not None) == expected, "historical membership oracle inconsistency")
                row["historical_boundary_probes"] = len(probes)
            # Verify a previously checked string also belongs to a NEW hypothesis.
            if stage == 1:
                _, data = load_bundle(bundle)
                witness = next(v for v in probes if history_contains(history, full_model, v) and
                               any(x["hypothesis"] == "flexible-independent" for x in explain(data, v)))
                row["new_hypothesis_reproduces_completed_witness"] = {"text": witness.decode(),
                    "contributions": explain(data, witness), "excluded": eligible.rank(witness) is None}
                require(row["new_hypothesis_reproduces_completed_witness"]["excluded"], "new explanation bypassed old coverage")

            attempts = (2, 3, 2, 3, 20)[stage]
            for attempt in range(attempts):
                with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                    start = c.state["cursor"]
                    submitted = min(unit + unit//5, c.status()["available_in_plan"])
                    require(submitted > 0, "unexpected plan depletion")
                    audit = audit_stream(core, auditor, c.blob(c.state["plan"]), oracle_path, start, submitted)
                    audited += submitted
                    # A committed result followed by a lost caller response must
                    # resume AFTER the committed prefix, not replay it.
                    lost_reply = stage == 1 and attempt == 1
                    check_began = time.monotonic()
                    try:
                        answer = c.run_batch(submitted, fault="after_commit" if lost_reply else None)
                    except RuntimeError as e:
                        require(lost_reply and "after durable commit" in str(e), "unexpected checker failure: " + str(e))
                        k = c.state["cursor"]-start
                        require(k == unit and c.state["hit"] is None and c.state["pending"] is None, "durable reply-loss state")
                        answer = {"status": "partial", "committed_negative": k, "generated_and_delivered": submitted,
                                  "confirmed_hits": 0, "uncredited_tail": submitted-k,
                                  "seconds": time.monotonic()-check_began, "injected_lost_reply_after_commit": True}
                    k = answer["committed_negative"]
                    history.add(eligible, start, k)
                    require(history.count == c.state["checked_negative"], "native receipts and independent compact history differ")
                    checked += k + answer["confirmed_hits"]
                    delivered += answer["generated_and_delivered"]
                    if k:
                        probes += [eligible.at(start)[0], eligible.at(start+k-1)[0]]
                    if start+k < eligible.count:
                        probes.append(eligible.at(start+k)[0])
                    if answer["status"] == "hit":
                        require(stage == 4 and eligible.at(start+k)[0] == password and c.state["hit"]["hex"] == password.hex(),
                                "unexpected/mismatched recovery")
                    else:
                        require(k == unit and c.state["cursor"] == start+k, "unfinished tail incorrectly credited")
                    row["batches"] += 1
                    batches.append({"stage": stage, "start": start, "audit": audit, **answer})
                    row["last_status"] = c.status()
                # Fresh owner after each batch; no Python in-memory cursor is needed.
                with CheckpointCampaign(campaign, checkpoint_every=0) as reopened:
                    require(reopened.state["cursor"] == start+k and reopened.state["pending"] is None,
                            "restart repeated committed work or skipped pending work")
                print(json.dumps({"stage": stage, "batch": attempt, "acknowledged_negatives": history.count,
                    "found": answer["status"] == "hit", "check_seconds": answer["seconds"],
                    "audited_frames": audit["frames_compared_exactly"]}), flush=True)
                if answer["status"] == "hit":
                    break
            require((row["last_status"]["hit"] is not None) == (stage == 4), "fixture did not complete its fixed clue schedule")
            with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                row["checkpoint"] = c.checkpoint()
                row["status"] = c.status()
            row["checked_after"] = history.count
            save_new(output / f"stage-{stage}.json", row)

        with CheckpointCampaign(campaign, checkpoint_every=0) as c:
            result["audit"] = c.audit()
            result["final_status"] = c.status()
            require(result["audit"]["verified"] and c.state["hit"]["hex"] == password.hex(), "final hit/history audit failed")
        require(stages[4]["target_eligible_rank"] < stages[3]["target_eligible_rank"], "new clue did not promote target")
        result.update(status="passed", actual_equality_checks=checked, acknowledged_distinct_negatives=history.count,
            repeated_acknowledged_negatives=0, actual_frames_delivered=delivered, audit_only_frames_compared=audited,
            independent_history_skeletons=len(history.ranges), independent_history_intervals=sum(map(len, history.ranges.values())),
            checker_pipeline_seconds=sum(b["seconds"] for b in batches),
            audit_pipeline_seconds=sum(b["audit"]["wall_seconds"] for b in batches),
            modeled_trillion_space_was_not_exhaustively_checked=True)
    except BaseException as error:
        result["failure"] = repr(error)
        raise
    finally:
        result["wall_seconds"] = time.monotonic()-began
        result["storage_before_report"] = budget(output)
        save_new(output / "results.json", result)
    lines = ["# Larger fuzzy-memory campaign", "", "Actual native CPU-equality checks only; no cryptography or real recovery history.",
        "", f"Artificial password: `{password.decode()}`. Fixed seed: {seed}. The clue builder never receives either.",
        "", "| Revision | Modeled distinct candidates | Completed overlaps excluded | Target eligible rank (zero-based) | Negatives after |",
        "| --- | ---: | ---: | ---: | ---: |"]
    for row in stages:
        lines.append(f"| {row['stage']}: {row['description']} | {row['full_candidates']:,} | {row['completed_overlaps_excluded']:,} | "
                     f"{row['target_eligible_rank'] if row['target_eligible_rank'] is not None else 'not admitted'} | {row['checked_after']:,} |")
    lines += ["", f"Recovered after **{checked:,} actual equality checks**. No acknowledged negative was checked again.",
        f"The independent C++ audit compared all {audited:,} delivered frames byte-for-byte with an independently computed recipe sequence.",
        "Audit passes do not open the password target and are not additional password checks. Deliberately unchecked delivery tails may be delivered again.",
        "The oracle represents numeric intervals per text skeleton; the worker separately stores immutable plans and receipt-backed rank intervals. Neither stores a list of all guesses.",
        "", "The misleading revision intentionally removed the correct construction. Restoring it preserved previous work. A newly added rule was also explicitly shown to produce an already checked string, which remained excluded.",
        "One injected lost response occurred AFTER durable commit. Restart resumed after that prefix. Loss before acknowledgment can still require safe repeated checks; this experiment does not promise otherwise.",
        "", f"Checker pipeline time across batches: {result['checker_pipeline_seconds']:.3f} s. Additional independent audit time: {result['audit_pipeline_seconds']:.3f} s. Total experiment: {result['wall_seconds']:.3f} s.",
        f"Artifacts before this report: {result['storage_before_report']['retained_bytes']:,} bytes. No candidate wordlists were written.",
        "", "Large modeled counts are counts/seeks, not trillions of completed checks. Deep windows validate selected exact probabilities and ranks; the full actual delivered stream is audited.",
        "This is an engineered correctness/performance experiment, not evidence that the real password lies in an affordable search. Later clues intentionally contain useful information about the true recipe.", ""]
    with (output / "REPORT.md").open("x") as f:
        (output / "REPORT.md").chmod(0o600)
        f.write("\n".join(lines))
    budget(output)
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("new_directory", type=Path)
    parser.add_argument("--seed", type=int, default=20261001)
    parser.add_argument("--batch-millions", type=int, default=10)
    args = parser.parse_args()
    run(args.new_directory, seed=args.seed, unit=args.batch_millions*1_000_000)
