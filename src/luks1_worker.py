"""Run a real LUKS1 CPU checker through the existing checkpointed worker.

This CLI deliberately creates only generated-control campaigns. The native
adapter itself reads any supported header; enabling a recovery campaign with
old Hashcat exclusions needs an explicit verifier/target migration, not a new
empty history. No old worker or evidence file is changed by this integration.
"""
from layout import BUILD
import argparse
import json
from pathlib import Path
import shutil

from checkpoint_runner import CheckpointCampaign
from model_workflow import CORE, read_json
from runner import HERE, execute, require, sha

BINARY = BUILD / "luks1-checker"


def commands(binary, qemu, *, purpose="control", slot="all", limit="all"):
    binary, qemu = Path(binary).resolve(strict=True), Path(qemu).resolve(strict=True)
    require(purpose in {"control", "recovery"}, "explicit target purpose required")
    require(slot == "all" or slot in {str(i) for i in range(8)}, "slot selection")
    require(limit == "all" or type(limit) is int and 0 <= limit <= 100000000, "prefix limit")
    return ([str(binary), "check", purpose, slot, str(limit)],
            [str(binary), "confirm-qemu", str(qemu), sha(qemu)])


def create_control(directory, plan, controls, *, name="high", binary=BINARY, qemu=None, limit="all", core=CORE):
    controls = Path(controls).resolve()
    doc = read_json(controls/"controls.json")
    require(doc.get("schema") == "synthetic-target-matched-luks1-v1" and doc.get("synthetic_only") is True,
            "an independently validated generated-control manifest is required")
    filename = f"SYNTHETIC-{name}.luks"
    require(name in {"low", "high"}, "control name")
    rows = [r for r in doc["controls"] if r["file"] == filename]
    require(len(rows) == 1 and rows[0]["sha256"] == sha(controls/filename), "control target changed")
    require(rows[0]["qemu_positive"]["accepted"] is True and rows[0]["qemu_negative"]["accepted"] is False,
            "control has no independent positive/negative qualification")
    qemu = Path(qemu or shutil.which("qemu-img") or "missing-qemu").resolve(strict=True)
    inspected = json.loads(execute([binary, "inspect", controls/filename]))
    require(inspected["target_sha256"] == rows[0]["sha256"], "adapter target mismatch")
    checker, confirmer = commands(binary, qemu, limit=limit)
    return CheckpointCampaign.create(directory, plan, controls/filename, checker, confirmer, core=core)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    verbs = p.add_subparsers(dest="command", required=True)
    c = verbs.add_parser("create-control")
    c.add_argument("directory", type=Path); c.add_argument("plan", type=Path); c.add_argument("controls", type=Path)
    c.add_argument("--name", choices=("low", "high"), default="high")
    c.add_argument("--qemu", type=Path, default=shutil.which("qemu-img"))
    for verb in ("run", "status", "revise", "audit", "checkpoint"):
        x = verbs.add_parser(verb); x.add_argument("directory", type=Path)
        if verb == "run":
            x.add_argument("--count", type=int, default=16)
            x.add_argument("--timeout", type=float, default=100)
        if verb == "revise": x.add_argument("plan", type=Path)
    a = p.parse_args()
    if a.command == "create-control":
        with create_control(a.directory, a.plan, a.controls, name=a.name, qemu=a.qemu) as campaign:
            result = campaign.status()
    else:
        with CheckpointCampaign(a.directory) as campaign:
            require(campaign.context["checker"]["argv"][1:3] == ["check", "control"], "not a LUKS control campaign")
            if a.command == "run":
                require(0 < a.timeout <= 3600, "timeout outside bounds")
                result = campaign.run_batch(a.count, timeout=a.timeout)
            elif a.command == "revise": result = campaign.revise(a.plan)
            else: result = getattr(campaign, a.command)()
    print(json.dumps({"purpose": "generated-control-only", "backend": "CPU-reference", "result": result}, indent=2))


if __name__ == "__main__": main()
