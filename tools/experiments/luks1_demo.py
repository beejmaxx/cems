"""Retain a small, reproducible real-LUKS control campaign and its evidence.

No mounting, root, Tails, GPU, rental, or real recovery-ledger writes. Optional
--smoke-template checks only the public control password against the supplied
template in a separate, unimported diagnostic. It does not search the real model.
"""
from layout import BUILD, source as source_path
import argparse
import hashlib
import itertools
import json
import os
from pathlib import Path
import shutil
import subprocess
import time

from checkpoint_runner import CheckpointCampaign
from fixtures import explicit_graph, write_source
import luks1_fixture as fixture
from luks1_worker import BINARY, create_control
from model_workflow import CORE, inspect
from runner import HERE, RESERVE, require, sha


def save(path, doc):
    fixture.write_new(path, (json.dumps(doc, indent=2, sort_keys=True)+"\n").encode())


def source_model(root, name, *, add_eight=False):
    drinks, periods = {b"Coffee": 4, b"Tea": 1}, {b"Morning": 4, b"Evening": 1}
    numbers = {b"7": 4, b"9": 2}
    if add_eight: numbers[b"8"] = 8
    masses = {b".".join((d,n,p)): drinks[d]*numbers[n]*periods[p]
              for d,n,p in itertools.product(drinks, numbers, periods)}
    source, plan = root/(name+".swg"), root/(name+".plan")
    write_source(source, {"construction": explicit_graph(masses)}, {"construction": 1})
    r = subprocess.run([str(CORE), "compile", str(source), str(plan)], capture_output=True, check=True, timeout=100)
    return plan, masses, json.loads(r.stdout)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("directory", type=Path)
    p.add_argument("--template", type=Path)
    p.add_argument("--smoke-template", action="store_true")
    a = p.parse_args()
    root = a.directory.resolve()
    require(not root.exists(), "new demonstration directory required")
    require(not a.smoke_template or a.template is not None, "smoke test requires explicit template")
    require(shutil.disk_usage(root.parent).free >= RESERVE+64*1024**2, "disk reserve")
    qemu = Path(shutil.which("qemu-img") or "missing-qemu").resolve(strict=True)
    protected = [source_path("runner.py"), source_path("checkpoint_runner.py"), source_path("history_campaign.py"), CORE,
                 BUILD / "synthetic-checker"]
    from workspace import ENVIRONMENT, Workspace
    import os
    if os.environ.get(ENVIRONMENT):
        ws = Workspace()
        protected += [ws.path("history") / "coverage.json", ws.path("history") / "completed.plan"]
    if a.template: protected.append(a.template.resolve())
    before = {str(f): sha(f) for f in protected if f.is_file()}
    root.mkdir(mode=0o700)
    start = time.monotonic()
    controls = fixture.build(root/"controls", template=a.template, qemu=qemu)
    old, old_values, old_info = source_model(root, "initial")
    revised, new_values, new_info = source_model(root, "new-number-memory", add_eight=True)
    require(fixture.PASSWORD not in old_values and fixture.PASSWORD in new_values, "demonstration model support")
    path = root/"SYNTHETIC-LUKS-campaign"
    with create_control(path, old, root/"controls", qemu=qemu, limit=2) as c:
        first = c.run_batch(8)
        require(first["status"] == "partial" and first["committed_negative"] == 2, "initial partial batch")
        completed = [value for value, _ in inspect(CORE, old, 0, 2)]
        save(root/"first-batch.json", first)
    with CheckpointCampaign(path) as c:
        initial_resume = c.status()
        revision = c.revise(revised)
        remaining = c.blob(c.state["plan"])
        rows = inspect(CORE, remaining, 0, len(new_values)-len(completed))
        remaining_values = [value for value, _ in rows]
        expected = sorted((v for v in new_values if v not in completed), key=lambda v: (-new_values[v], v))
        require(remaining_values == expected and not set(completed) & set(remaining_values),
                "revision lost ranking or repeated completed candidates")
        found = c.run_batch(16)
        require(found["status"] == "hit" and c.state["hit"]["hex"] == fixture.PASSWORD.hex(), "known password not recovered")
        c.checkpoint(); audit = c.audit(); status = c.status()
    with CheckpointCampaign(path) as c:
        require(c.status()["hit"] == status["hit"], "confirmed hit did not survive reopening")
        final_audit = c.audit()
    # No workload dependent on data-entropy: same header/keyslots, both payloads.
    control_checks = []
    for row in controls["controls"]:
        target = root/"controls"/row["file"]
        raw = b"".join(bytes([len(v)])+v for v in (b"known-wrong", fixture.PASSWORD, b"untouched-tail"))
        result = subprocess.run([str(BINARY), "check", "control", "all", "all", str(target),
            "a"*32, "b"*64, sha(target), "3"], input=raw, capture_output=True, check=True, timeout=30)
        ack = json.loads(result.stdout)
        require(ack["status"] == "hit" and ack["negative_prefix"] == 1, "entropy-control check failed")
        control_checks.append({"control": row["file"], "receipt": ack})
    smoke = None
    if a.smoke_template:
        target = a.template.resolve(); digest = sha(target)
        result = subprocess.run([str(BINARY), "check", "recovery", "all", "all", str(target),
            "c"*32, "d"*64, digest, "1"], input=bytes([len(fixture.PASSWORD)])+fixture.PASSWORD,
            capture_output=True, check=True, timeout=30)
        ack = json.loads(result.stdout)
        py_match = fixture.unwrap(target.read_bytes(), fixture.PASSWORD) is not None
        qemu_check = fixture.qemu_read(target, fixture.PASSWORD, qemu)
        require((ack["status"] == "hit") == py_match == qemu_check["accepted"], "real-template independent smoke mismatch")
        smoke = {"target_sha256": digest, "public_probe_hex": fixture.PASSWORD.hex(), "native": ack,
                 "python_digest_accepts": py_match, "qemu": qemu_check,
                 "imported_as_recovery_coverage": False,
                 "meaning": "One public control-password probe, not qualification from a real-target known-positive"}
    after = {f: sha(f) for f in before}
    require(before == after, "protected source/header/evidence changed")
    report = {"schema": "native-luks1-adapter-demonstration-v1", "purpose": "generated-control-only",
        "backend": "CPU-reference; not a GPU throughput qualification",
        "controls": controls, "initial_model": old_info, "revised_model": new_info,
        "initial_support": len(old_values), "revised_support": len(new_values),
        "initial_resume": initial_resume, "first_batch": first, "completed_before_revision_hex": [v.hex() for v in completed],
        "revision": revision, "next_eligible_hex": [v.hex() for v in remaining_values], "recovery": found,
        "final_status": status, "audit": audit, "fresh_reopen_audit": final_audit,
        "independent_confirmation": "QEMU unlock plus one plaintext sector read; executable SHA pinned",
        "entropy_controls": control_checks, "template_smoke": smoke,
        "protected_files_unchanged": before, "seconds": time.monotonic()-start,
        "artifacts_bytes": sum(f.stat().st_size for f in root.rglob("*") if f.is_file()),
        "build_identities": {str(f.relative_to(HERE)): sha(f) for f in
             (BINARY, source_path("luks1_checker.cpp"), source_path("luks1_fixture.py"), source_path("luks1_worker.py"), source_path("luks1_demo.py"))},
        "qemu_version": subprocess.check_output([str(qemu), "--version"], text=True).strip(),
        "limitations": ["Finite CPU reference qualification, not a proof of all possible inputs or GPU execution",
            "Old Hashcat entropy-based negatives are not automatically upgraded to digest-based evidence",
            "No recovery campaign/ledger created; generated targets have different identities",
            "QEMU confirmer needs a readable payload sector and cannot confirm embedded-NUL passphrases",
            "Only AES-256 CBC-ESSIV:SHA256 / SHA1 LUKS1 is admitted; unsupported formats fail closed"]}
    require(report["artifacts_bytes"] <= 64*1024**2, "demonstration storage bound")
    save(root/"results.json", report)
    text = ("# Real LUKS1 checker demonstration\n\n"
        "The C++ worker recovered the public control password `Coffee.8.Morning` from a generated LUKS1 image. "
        "QEMU independently unlocked it. This is real PBKDF2/AES/AF-merge/master-key-digest checking, not string equality.\n\n"
        "The control matches the supplied template's cipher, hash, key size, both iteration counts, all slot activation flags/offsets/stripe counts, "
        "and payload offset. Salts, UUID, volume key, digest and ciphertext are newly generated. No original encrypted data is copied.\n\n"
        "Two initial wrong guesses were acknowledged, the campaign was closed/reopened, an overlapping revision added the number 8, "
        "and both acknowledged overlaps were excluded. The password was recovered and independently confirmed; the untouched tail was not credited.\n\n"
        "Low-entropy and 8-bit/byte plaintext controls share identical header/keyslot bytes and both pass. "
        "The original header, frozen engine/controller and imported coverage files retain their hashes.\n\n"
        "This qualifies a CPU correctness reference. It is not a GPU bridge, eight-GPU benchmark, or a migration of prior Hashcat evidence. "
        "See `results.json` for exact identities, receipts, profile fields and limitations.\n")
    fixture.write_new(root/"REPORT.md", text.encode())
    print(json.dumps({"directory": str(root), "recovered_control": fixture.PASSWORD.decode(),
        "matched_profile": controls["profile"], "original_unchanged": True, "seconds": report["seconds"],
        "bytes": report["artifacts_bytes"], "template_smoke_status": smoke["native"]["status"] if smoke else None}, indent=2))


if __name__ == "__main__": main()
