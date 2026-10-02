"""Exercise real Hashcat receipts and process-loss recovery on generated targets.

Requires a successful GPU qualification from this host. Never accepts a real
target, personal model, or existing campaign. Each phase opens the campaign in
a separate process; deliberate os._exit calls bypass Python cleanup, but are
not power-loss tests. The Hashcat backend and verifier are never mocked.
"""
import argparse
import json
import os
from pathlib import Path
import subprocess
import sys
import time

import gpu_backend as gpu
import gpu_worker as worker
import luks1_fixture as fixture
from layout import LAB
from model_workflow import CORE, inspect, read_json, save_new
from runner import canonical, disk_guard, require, sha, sync_dir

SCHEMA = "synthetic-gpu-campaign-qualification-v1"
CRASH_EXIT = 73
WRONG = [b"control-wrong-a", b"control-wrong-b", b"control-wrong-c"]
HIT_VALUES = [*WRONG[:2], fixture.PASSWORD, b"control-uncredited-tail"]
REVISION_VALUES = [b"control-new", WRONG[1], WRONG[0], WRONG[2]]


def snapshot(campaign):
    audit = campaign.audit()
    completed = []
    for plan, ranges in campaign.state["ranges"].items():
        for first, end in ranges:
            require(0 < end-first <= 8, "synthetic coverage bound")
            completed.extend(value.hex() for value, _ in inspect(CORE, campaign.blob(plan), first, end-first))
    require(len(completed) == len(set(completed)) == campaign.state["checked_negative"],
            "duplicate or missing completed synthetic candidates")
    size = int(campaign.info(campaign.state["plan"])["candidates"])
    require(size <= 8, "synthetic plan bound")
    active = [value.hex() for value, _ in inspect(CORE, campaign.blob(campaign.state["plan"]), 0, size)] if size else []
    receipts = [json.loads(row[0]) for row in campaign.db.execute("SELECT body FROM tail ORDER BY seq")]
    discoveries = {p.name: read_json(p) for p in sorted((campaign.root/"discoveries").glob("*.json"))}
    residue = {str(p.relative_to(campaign.root)): sha(p)
               for directory in campaign.root.glob("gpu-batch-*")
               for p in sorted(directory.rglob("*")) if p.is_file()}
    return {"state": campaign.state, "head": campaign.head, "events": campaign.sequence,
            "audit": audit, "completed_hex": sorted(completed), "active_plan_hex": active,
            "tail_completions": [doc for doc in receipts if doc["kind"] == "gpu-complete"],
            "discoveries": discoveries, "temporary_residue": residue,
            "checkpoint_through": campaign.through, "audit_archives": len(campaign.archives)}


def die(campaign, phase, output):
    save_new(output, {"phase": phase, "exit_code": CRASH_EXIT, "snapshot": snapshot(campaign)})
    sync_dir(output.parent)
    os._exit(CRASH_EXIT)  # Deliberately skips campaign.close() and TemporaryDirectory cleanup.


class DiscoveryInterruption(worker.GpuControlCampaign):
    def preserve_discovery(self, job, index, value, proof):
        super().preserve_discovery(job, index, value, proof)
        die(self, "after_discovery", self.interruption_output)


def child(directory, phase, output, count, timeout):
    directory, output = Path(directory).resolve(), Path(output).resolve()
    require(output.parent == directory and not output.exists(), "new case-local phase result required")
    spec = read_json(directory/"synthetic-case.json")
    require(spec.get("schema") == SCHEMA and spec.get("synthetic_only") is True, "synthetic case required")
    cls = DiscoveryInterruption if phase == "after_discovery" else worker.GpuControlCampaign
    with cls(directory/"campaign") as campaign:
        require(campaign.configuration()["header_sha256"] == spec["header_sha256"] and
                campaign.context["target"] == spec["target_sha256"], "synthetic case target changed")
        campaign.interruption_output = output
        before = snapshot(campaign)
        if phase in {"run", "before_commit", "after_commit", "after_discovery"}:
            try:
                result = campaign.run_batch(count, timeout=timeout,
                    fault=phase if phase in {"before_commit", "after_commit"} else None)
            except RuntimeError as error:
                expected = {"before_commit": "injected loss after GPU result before commit",
                            "after_commit": "injected loss after durable GPU completion"}
                require(phase in expected and str(error) == expected[phase], str(error))
                die(campaign, phase, output)
            require(phase == "run", "expected process interruption was not reached")
        elif phase == "checkpoint":
            result = campaign.checkpoint()
        elif phase == "revise":
            model = directory/"revision-model"; model.mkdir(mode=0o700)
            result = campaign.revise(worker.controlled_plan(model, REVISION_VALUES))
        else:
            require(phase == "inspect", "unknown phase")
            result = None
        save_new(output, {"phase": phase, "before": before, "result": result, "snapshot": snapshot(campaign)})


def phase(directory, name, operation, *, count=1, timeout=90):
    output = directory/(name+".json")
    argv = [sys.executable, "-B", str(LAB/"search.py"), "gpu-campaign-qualify", str(directory),
            "--_phase", operation, "--_result", str(output), "--_count", str(count), "--timeout", str(timeout)]
    began = time.monotonic()
    with (directory/(name+".log")).open("xb") as log:
        result = subprocess.run(argv, stdout=log, stderr=subprocess.STDOUT, timeout=4*timeout+120)
    expected = CRASH_EXIT if operation in {"before_commit", "after_commit", "after_discovery"} else 0
    save_new(directory/(name+"-process.json"), {"argv": argv, "returncode": result.returncode,
             "expected_returncode": expected, "seconds": time.monotonic()-began})
    require(result.returncode == expected, "phase failed; inspect "+str(directory/(name+".log")))
    doc = read_json(output)
    require(doc["snapshot"]["audit"]["verified"] is True, "phase audit failed")
    print(directory.name+"/"+name+": PASS", flush=True)
    return doc


def create_case(root, name, values, cfg):
    directory = root/name; directory.mkdir(mode=0o700)
    model = directory/"model"; model.mkdir(mode=0o700)
    plan = worker.controlled_plan(model, values)
    target = root/"controls/SYNTHETIC-low.hash"
    save_new(directory/"synthetic-case.json", {"schema": SCHEMA, "synthetic_only": True,
        "header_sha256": cfg["header_sha256"], "target_sha256": cfg["target_sha256"],
        "candidate_hex": [value.hex() for value in values], "plan_sha256": sha(plan)})
    with worker.GpuControlCampaign.create(directory/"campaign", plan, target,
            [cfg["runtime"]["executable"], gpu.MARKER, canonical(cfg).decode()],
            [cfg["native"]["path"], "confirm-qemu", cfg["qemu"]["path"], cfg["qemu"]["sha256"]],
            core=CORE) as campaign:
        require(campaign.state["receipts"] == 0, "new campaign already has receipts")
    return directory


def expect_coverage(doc, values, *, hit=False):
    snap = doc["snapshot"]; state = snap["state"]
    require(snap["completed_hex"] == sorted(value.hex() for value in values), "completed candidate set differs")
    require(state["checked_negative"] == len(values), "negative count differs")
    if hit:
        require(state["hit"] is not None and state["hit"]["hex"] == fixture.PASSWORD.hex() and
                state["hit"]["rank"] == 2 and state["pending"] is None, "confirmed hit not retained")
    else:
        require(state["hit"] is None, "unexpected synthetic hit")


def expect_discovery(doc):
    discoveries = list(doc["snapshot"]["discoveries"].values())
    require(len(discoveries) == 1, "confirmed discovery missing or duplicated")
    discovery = discoveries[0]
    require(discovery["hit_hex"] == fixture.PASSWORD.hex() and discovery["rank"] == 2 and
            discovery["negative_coverage_claimed"] == 0 and discovery["confirmation"]["native_digest"] is True and
            discovery["confirmation"]["qemu"]["confirmed"] is True, "discovery evidence differs")


def finish_case(directory, values, *, hit=False, timeout=90):
    checkpoint = phase(directory, "checkpoint", "checkpoint", timeout=timeout)
    reopened = phase(directory, "reopened", "inspect", timeout=timeout)
    expect_coverage(reopened, values, hit=hit)
    require(checkpoint["snapshot"]["state"] == reopened["snapshot"]["state"] and
            checkpoint["snapshot"]["head"] == reopened["snapshot"]["head"] and
            reopened["snapshot"]["audit_archives"] >= 1, "checkpoint/reopen changed receipt state")
    if hit: expect_discovery(reopened)
    return {"case": directory.name, "passed": True, "final": reopened["snapshot"],
            "evidence": str(directory)}


def qualify(directory, qualification, *, timeout=90):
    require(1 <= timeout <= 3600, "timeout bound")
    root = Path(directory).resolve(); qualification = Path(qualification).resolve(strict=True)
    require(not root.exists() and root.parent.is_dir(), "new output directory with an existing parent required")
    q = read_json(qualification)
    require(q.get("schema") == "bounded-gpu-qualification-v1" and q.get("ordinary_controls_passed") is True,
            "successful hardware qualification required")
    root.mkdir(mode=0o700); disk_guard(root, 128*1024**2)
    began = time.monotonic(); harness_sha = sha(__file__)
    fixture.build(root/"controls", qemu=Path(q["qemu"]["path"]))
    header, target = root/"controls/SYNTHETIC-low.luks", root/"controls/SYNTHETIC-low.hash"
    fixture.write_new(target, gpu.extract(header.read_bytes()))
    cfg = worker.checker_config(qualification, header, target)
    save_new(root/"run.json", {"schema": SCHEMA, "synthetic_only": True,
             "qualification": str(qualification), "qualification_sha256": sha(qualification),
             "harness_sha256": harness_sha, "timeout": timeout, "config": cfg})
    cases = []

    for name, interruption in (("hit", None), ("hit-before-commit", "before_commit"),
                               ("hit-after-discovery", "after_discovery")):
        case = create_case(root, name, HIT_VALUES, cfg)
        pending = residue = None
        if interruption:
            stopped = phase(case, "interrupted", interruption, count=4, timeout=timeout)
            expect_coverage(stopped, []); expect_discovery(stopped)
            pending = stopped["snapshot"]["state"]["pending"]
            require(pending and pending["count"] == 4 and stopped["snapshot"]["state"]["receipts"] == 0,
                    "uncommitted hit received credit")
            resumed = phase(case, "before-retry", "inspect", timeout=timeout)
            require(resumed["snapshot"]["state"] == stopped["snapshot"]["state"], "pending hit changed on reopen")
            expect_discovery(resumed)
            residue = resumed["snapshot"]["temporary_residue"]
            require(bool(residue) == (interruption == "after_discovery"), "unexpected crash residue")
        found = phase(case, "recovered", "run", count=1 if pending else 4, timeout=timeout)
        expect_coverage(found, WRONG[:2], hit=True); expect_discovery(found)
        if pending: require(found["result"]["job"] == pending, "retry replaced the pending assignment")
        require(found["snapshot"]["temporary_residue"] == (residue or {}), "normal completion leaked temporary files")
        receipt, = found["snapshot"]["tail_completions"]
        require(receipt["ack"]["status"] == "hit" and receipt["ack"]["hit_hex"] == fixture.PASSWORD.hex() and
                receipt["ack"]["submitted"] == 4 and receipt["ack"]["negative_prefix"] == 2 and
                [item["status"] for item in receipt["proof"]["attempts"]] == [6, 5] and
                receipt["proof"]["attempts"][-1]["candidate_count"] == 2,
                "hit receipt lacks independently exhausted earlier prefix")
        cases.append(finish_case(case, WRONG[:2], hit=True, timeout=timeout))

    case = create_case(root, "negative-before-commit", WRONG, cfg)
    stopped = phase(case, "interrupted", "before_commit", count=2, timeout=timeout)
    expect_coverage(stopped, [])
    require(stopped["snapshot"]["state"]["receipts"] == 0, "uncommitted negative received a receipt")
    retried = phase(case, "retried", "run", count=1, timeout=timeout)
    require(retried["result"]["job"] == stopped["snapshot"]["state"]["pending"], "negative retry changed the assignment")
    expect_coverage(retried, WRONG[:2])
    completed = phase(case, "completed", "run", count=1, timeout=timeout)
    expect_coverage(completed, WRONG)
    depleted = phase(case, "depleted", "run", timeout=timeout)
    require(depleted["result"]["status"] == "plan_depleted" and
            depleted["before"]["head"] == depleted["snapshot"]["head"], "depleted plan dispatched more work")
    cases.append(finish_case(case, WRONG, timeout=timeout))

    case = create_case(root, "negative-after-commit-revision", WRONG, cfg)
    stopped = phase(case, "interrupted", "after_commit", count=2, timeout=timeout)
    expect_coverage(stopped, WRONG[:2])
    require(stopped["snapshot"]["state"]["pending"] is None and stopped["snapshot"]["state"]["receipts"] == 1,
            "committed receipt was lost")
    resumed = phase(case, "before-revision", "inspect", timeout=timeout)
    require(resumed["snapshot"]["state"] == stopped["snapshot"]["state"], "committed state changed on reopen")
    revised = phase(case, "revised", "revise", timeout=timeout)
    remaining = [REVISION_VALUES[0], WRONG[2]]
    require(revised["snapshot"]["active_plan_hex"] == [value.hex() for value in remaining],
            "revision changed ranking or repeated completed candidates")
    expect_coverage(revised, WRONG[:2])
    completed = phase(case, "completed", "run", count=2, timeout=timeout)
    expect_coverage(completed, [*WRONG, REVISION_VALUES[0]])
    require(completed["result"]["job"]["count"] == 2 and completed["snapshot"]["state"]["receipts"] == 2,
            "revision repeated committed work")
    cases.append(finish_case(case, [*WRONG, REVISION_VALUES[0]], timeout=timeout))

    require(sha(__file__) == harness_sha, "qualification harness changed while running")
    worker.checker_config(qualification, header, target)
    report = {"schema": SCHEMA, "synthetic_only": True, "all_cases_passed": True,
        "real_target_checks": 0, "negative_policy": gpu.POLICY, "digest_equivalence_established": False,
        "hardware_qualification": str(qualification), "hardware_qualification_sha256": sha(qualification),
        "runtime": cfg["runtime"], "devices": cfg["devices"], "harness_sha256": harness_sha,
        "seconds": time.monotonic()-began, "cases": cases,
        "limitations": ["Generated targets and small candidate sets only; not a throughput benchmark",
            "Abrupt process exits, not power-loss or distributed-failure tests",
            "Stock Hashcat payload-entropy negatives retain their known false-negative limitation",
            "Other hardware and driver installations require their own qualification"]}
    save_new(root/"campaign-qualification.json", report)
    print(json.dumps({"report": str(root/"campaign-qualification.json"), "all_cases_passed": True,
                      "cases": len(cases), "seconds": report["seconds"]}, indent=2))
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--qualification", type=Path, help="successful local gpu qualify report")
    p.add_argument("--timeout", type=int, default=90, help="per-Hashcat invocation seconds")
    p.add_argument("--_phase", help=argparse.SUPPRESS)
    p.add_argument("--_result", type=Path, help=argparse.SUPPRESS)
    p.add_argument("--_count", type=int, default=1, help=argparse.SUPPRESS)
    a = p.parse_args()
    os.umask(0o077)
    if a._phase:
        require(a._result is not None, "internal phase result required")
        child(a.directory, a._phase, a._result, a._count, a.timeout)
    else:
        require(a.qualification is not None, "supply --qualification from this GPU host")
        qualify(a.directory, a.qualification, timeout=a.timeout)


if __name__ == "__main__":
    main()
