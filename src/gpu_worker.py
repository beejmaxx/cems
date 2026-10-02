"""Single-owner GPU handoff: bounded native preparation, conservative completion.

Explicitly uses stock Hashcat's payload-entropy policy, NOT digest-equivalent
negatives. Positive hits require the native digest check AND independent QEMU.
"""
from layout import BUILD, source as source_path
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import time
import uuid

import gpu_backend as gpu
import history_campaign as history
import coverage_snapshot as coverage
import luks1_fixture as fixture
from checkpoint_runner import CheckpointCampaign, parse
from fixtures import explicit_graph, write_source
from model_workflow import CORE, save_new, read_json
from runner import HERE, canonical, disk_guard, execute, require, sha, sync_dir

NATIVE = BUILD / "luks1-checker"


def confirm(header, value, *, native, qemu):
    digest = sha(header); frame = bytes([len(value)])+value
    ack = parse(execute([native, "check", "recovery", "0", "all", header,
                        "a"*32, "b"*64, digest, "1"], data=frame, timeout=60))
    require(ack["status"] == "hit" and ack["negative_prefix"] == 0 and ack["hit_hex"] == value.hex(),
            "Hashcat hit failed the LUKS master-key digest; no coverage committed")
    q = parse(execute([native, "confirm-qemu", qemu, sha(qemu), header, digest], data=frame, timeout=60))
    require(q == {"schema": "confirmation-v1", "target": digest, "confirmed": True}, "QEMU did not confirm hit")
    return {"header_sha256": digest, "native_digest": True, "qemu": q}


def controlled_plan(directory, values, core=CORE):
    require(len(values) == len(set(values)), "control candidates must be unique")
    source, plan = directory/"control.swg", directory/"control.plan"
    write_source(source, {"control": explicit_graph({v: len(values)-i for i,v in enumerate(values)})}, {"control": 1})
    execute([core, "compile", source, plan], file_cap=64*1024**2)
    return plan


def qualify(directory, *, executable, runtime_root, selected="all", timeout=180, qemu=None):
    directory = Path(directory).resolve(); directory.mkdir(mode=0o700, exist_ok=False)
    disk_guard(directory, 64*1024**2)
    runtime = gpu.runtime_identity(executable, runtime_root)
    gpu.devices(selected)
    qemu = Path(qemu or shutil.which("qemu-img") or "missing-qemu").resolve(strict=True)
    controls = fixture.build(directory/"controls", qemu=qemu)
    backend = execute([runtime["executable"], "-I"], timeout=30)
    fixture.write_new(directory/"backend.txt", backend)
    low, high = (directory/"controls"/f"SYNTHETIC-{name}.luks" for name in ("low", "high"))
    for header in (low, high): fixture.write_new(header.with_suffix(".hash"), gpu.extract(header.read_bytes()))
    negatives = [b"wrong one", b"$HEX[4142]:\\!", b"z"*64]
    public = fixture.PASSWORD
    cases = [("correct-only", low, [public], True, None),
             ("wrong-only", low, negatives, False, None),
             ("mixed-middle", low, [negatives[0], public, negatives[1]], True, None),
             ("mixed-last", low, negatives+[public], True, None),
             ("prefix-limit", low, negatives+[public], False, len(negatives))]
    records = []

    def run_case(name, header, values, expected, limit=None, choice=selected):
        d = directory/name; d.mkdir(mode=0o700)
        plan = controlled_plan(d, values)
        wordlist = d/"candidates.hex"
        delivered = gpu.spool(CORE, gpu.SPOOL, plan, 0, len(values), wordlist)
        result = gpu.hashcat_run(runtime, header.with_suffix(".hash"), wordlist, limit or len(values),
                                d/"check", selected=choice, timeout=timeout, limit=limit)
        found = result["found_hex"]
        good = (found == [values[-1].hex()] if name.startswith("device-") else
                found == [public.hex()] if expected else found == [])
        if name == "edges-64": good = found == [values[0].hex()]
        if found:
            proof = confirm(header, bytes.fromhex(found[0]), native=NATIVE, qemu=qemu)
        else: proof = None
        row = {"case": name, "passed": good, "result": result, "delivery": delivered, "confirmation": proof}
        records.append(row); save_new(d/"result.json", row)
        print(name+": "+("PASS" if good else "MISS"), flush=True)
        return row

    for name, header, values, positive, limit in cases:
        require(run_case(name, header, values, positive, limit)["passed"], "GPU baseline control failed")
    participating = records[0]["result"]["devices"]
    require(all(r["result"]["devices"] == participating for r in records), "GPU device selection changed across controls")
    if selected != "all":
        require({d["device_id"] for d in participating} == {int(v) for v in selected.split(",")},
                "not every requested GPU participated")
    for device in participating:
        identifier = str(device["device_id"])
        row = run_case("device-"+identifier, low, [public], True, choice=identifier)
        require(row["passed"] and row["result"]["devices"] == [device], "individual GPU did not pass control")
    edges = (b" !\\:$HEX[4142]"+b"Ab9!"*20)[:64]
    data, _ = fixture.image(fixture.default_profile(), edges, payload=bytes(512))
    edge_header = directory/"controls/SYNTHETIC-edges.luks"
    fixture.write_new(edge_header, data); fixture.write_new(edge_header.with_suffix(".hash"), gpu.extract(data))
    require(fixture.qemu_read(edge_header, edges, qemu)["accepted"], "independent edge control failed")
    require(run_case("edges-64", edge_header, [edges], True)["passed"], "64-byte delivery control failed")
    diagnostic = run_case("high-entropy", high, [public], True)
    # Missing this correct password is a documented counterexample for the
    # entropy heuristic. It does NOT silently become a digest-equivalent gate.
    report = {"schema": "bounded-gpu-qualification-v1", "synthetic_only": True,
              "ordinary_controls_passed": True, "high_entropy_passed": diagnostic["passed"],
              "negative_policy": gpu.POLICY, "digest_equivalence_established": False,
              "real_target_checks": 0, "runtime": runtime, "selected": selected,
              "devices": participating, "backend_sha256": hashlib.sha256(backend).hexdigest(),
              "qemu": {"path": str(qemu), "sha256": sha(qemu)},
              "native": {"path": str(NATIVE.resolve()), "sha256": sha(NATIVE)},
              "spool": {"path": str(gpu.SPOOL.resolve()), "sha256": sha(gpu.SPOOL)},
              "source_hashes": {p.name: sha(p) for p in (Path(__file__), source_path("gpu_backend.py"), source_path("hex_spool.cpp"))},
              "cases": records}
    save_new(directory/"qualification.json", report)
    return {"qualification": str(directory/"qualification.json"), "legacy_checker_controls_passed": True,
            "high_entropy_passed": diagnostic["passed"], "digest_equivalence_established": False}


def checker_config(qualification, header, target):
    qpath = Path(qualification).resolve(strict=True); q = read_json(qpath)
    require(q.get("schema") == "bounded-gpu-qualification-v1" and q.get("synthetic_only") is True and
            q.get("ordinary_controls_passed") is True and q.get("negative_policy") == gpu.POLICY and
            q.get("real_target_checks") == 0 and q.get("digest_equivalence_established") is False,
            "successful local GPU controls are required")
    required = {"correct-only", "wrong-only", "mixed-middle", "mixed-last", "prefix-limit", "edges-64"}
    required |= {"device-"+str(d["device_id"]) for d in q["devices"]}
    passed = {r["case"] for r in q["cases"] if r["passed"] is True}
    require(required <= passed and any(r["case"] == "high-entropy" for r in q["cases"]), "incomplete qualification")
    bound = gpu.bind_target(header, target)
    gpu.verify_runtime(q["runtime"])
    for kind in ("native", "spool", "qemu"):
        require(sha(q[kind]["path"]) == q[kind]["sha256"], "qualified executable changed")
    require(all(sha(source_path(n)) == d for n,d in q["source_hashes"].items()), "qualified adapter source changed")
    return {"schema": gpu.MARKER, "policy": gpu.POLICY, "qualification": str(qpath),
            "qualification_sha256": sha(qpath), "header": str(Path(header).resolve()), **bound,
            "runtime": q["runtime"], "selected": q["selected"], "devices": q["devices"],
            "native": q["native"], "qemu": q["qemu"], "spool": q["spool"], "source_hashes": q["source_hashes"]}


class GpuMixin:
    def preserve_discovery(self, job, index, value, proof):
        """A confirmed secret must survive even if prefix accounting later fails.

        This artifact does NOT assert that earlier/later guesses were checked.
        """
        doc = {"schema": "confirmed-hit-discovery-v1", "job": job, "rank": job["start"]+index,
               "hit_hex": value.hex(), "confirmation": proof,
               "target": self.context["target"], "negative_coverage_claimed": 0}
        data = canonical(doc); name = hashlib.sha256(data).hexdigest()+".json"
        directory = self.root/"discoveries"
        disk_guard(self.root, 2*len(data)+8192)
        directory.mkdir(mode=0o700, exist_ok=True); sync_dir(self.root)
        destination = directory/name
        if destination.exists():
            require(read_json(destination) == doc, "conflicting hit artifact")
        else:
            # Publish only a complete, durable private artifact. Uncommitted
            # negative work stays pending independently of this discovery.
            fd, temp = tempfile.mkstemp(prefix=".hit-", dir=directory)
            try:
                with os.fdopen(fd, "wb") as out:
                    out.write(data); out.flush(); os.fsync(out.fileno())
                os.link(temp, destination); sync_dir(directory)
            finally:
                Path(temp).unlink(missing_ok=True)
        return destination

    def configuration(self):
        args = self.context["checker"]["argv"]
        require(len(args) == 3 and args[1] == gpu.MARKER, "not a bounded GPU campaign")
        cfg = parse(args[2])
        require(cfg["schema"] == gpu.MARKER and cfg["policy"] == gpu.POLICY and
                cfg["target_sha256"] == self.context["target"], "GPU target/policy mismatch")
        return cfg

    def verify_runtime(self):
        super().verify_runtime()
        cfg = self.configuration()
        require(sha(cfg["qualification"]) == cfg["qualification_sha256"], "GPU qualification changed/missing")
        require(all(sha(source_path(n)) == d for n,d in cfg["source_hashes"].items()), "GPU adapter changed; explicit migration required")
        for kind in ("native", "qemu", "spool"):
            require(sha(cfg[kind]["path"]) == cfg[kind]["sha256"], "qualified executable changed")
        require(gpu.bind_target(cfg["header"], self.blob(self.context["target"], "targets")) ==
                {k: cfg[k] for k in ("header_sha256", "target_sha256")}, "GPU header/extract mismatch")
        gpu.verify_runtime(cfg["runtime"])

    def transition(self, doc):
        if doc.get("kind") == "complete":
            raise RuntimeError("GPU completion needs native delivery and terminal-checker evidence")
        if doc.get("kind") != "gpu-complete": return super().transition(doc)
        require(set(doc) == {"kind", "ack", "confirmation", "proof"} and self.state["pending"], "GPU completion fields")
        a, p, job = doc["ack"], doc["proof"], self.state["pending"]
        require(p["policy"] == gpu.POLICY and p["delivery"]["frames"] == job["count"] and
                0 < p["delivery"]["bytes"] <= job["count"]*129 and
                len(p["delivery"]["sha256"]) == 64, "GPU delivery proof")
        attempts = p["attempts"]
        require(attempts and len(attempts) <= 4, "GPU attempts bound")
        for item in attempts:
            n = item["candidate_count"]
            require(0 < n <= job["count"] and item["rejected"] == 0 and
                    item["devices"] == self.configuration()["devices"], "GPU result count/devices/rejection")
            if item["status"] == 5:
                require(item["returncode"] == 1 and item["progress"] == [n,n] and
                        item["recovered_hashes"] == [0,1] and item["found_hex"] == [], "GPU exhaustion proof")
            else:
                require(item["status"] == 6 and item["returncode"] == 0 and item["recovered_hashes"] == [1,1] and
                        len(item["found_hex"]) == 1, "GPU hit proof")
        k = a["negative_prefix"]
        if a["status"] == "negative":
            require(len(attempts) == 1 and attempts[0]["status"] == 5 and attempts[0]["candidate_count"] == k == job["count"]
                    and p["confirmation"] is None, "negative proof does not cover assignment")
        else:
            require(a["status"] == "hit" and p["confirmation"]["native_digest"] is True and
                    p["confirmation"]["header_sha256"] == self.configuration()["header_sha256"] and
                    p["confirmation"]["qemu"] == {"schema": "confirmation-v1", "target": self.configuration()["header_sha256"], "confirmed": True},
                    "missing independent hit evidence")
            require(any(a["hit_hex"] in r["found_hex"] for r in attempts), "hit missing from checker evidence")
            require(k == 0 or attempts[-1]["status"] == 5 and attempts[-1]["candidate_count"] == k,
                    "parallel Hashcat progress is not a negative prefix")
            expected = self.core("inspect", self.blob(job["plan"]), job["start"]+k, 1).decode().split()[0]
            require(expected == a["hit_hex"], "hit rank not bound to plan")
        return super().transition({"kind": "complete", "ack": a, "confirmation": doc["confirmation"]})

    def run_batch(self, count=1_000_000, *, timeout=600, fault=None):
        self.usable(); self.verify_runtime()
        if isinstance(self, history.HistoryCampaign): self.require_unresolved()
        require(type(count) is int and 0 < count <= gpu.MAX_BATCH, "GPU batches are bounded to one million")
        require(1 <= timeout <= 3600 and fault in (None, "before_commit", "after_commit"), "run bounds")
        require(self.state["hit"] is None, "campaign already has a confirmed hit")
        s = self.state
        if s["pending"] is None:
            count = min(count, int(self.info(s["plan"])["candidates"])-s["cursor"])
            if not count: return {"status": "plan_depleted", **self.status()}
            self.append({"kind": "dispatch", "job": {"id": uuid.uuid4().hex, "revision": s["revision"],
                "plan": s["plan"], "start": s["cursor"], "count": count}})
        job, cfg = self.state["pending"], self.configuration()
        require(job["count"] <= gpu.MAX_BATCH, "pending job exceeds GPU bound")
        disk_guard(self.root, job["count"]*129+32*1024**2)
        require(sha(self.blob(job["plan"])) == job["plan"], "active plan changed")
        began = time.monotonic(); proof = None
        with tempfile.TemporaryDirectory(prefix="gpu-batch-", dir=self.root) as temporary:
            work = Path(temporary); wordlist = work/"candidates.hex"
            delivery = gpu.spool(self.context["core"]["argv"][0], cfg["spool"]["path"],
                                 self.blob(job["plan"]), job["start"], job["count"], wordlist)
            attempts = []; to_check = job["count"]; hit = None; hit_proof = None
            while True:
                require(len(attempts) < 4, "multiple unexpected valid hits; manual review required")
                result = gpu.hashcat_run(cfg["runtime"], self.blob(self.context["target"], "targets"), wordlist,
                    to_check, work/("check-"+str(len(attempts))), selected=cfg["selected"], timeout=timeout,
                    limit=None if not attempts else to_check)
                attempts.append(result)
                require(result["devices"] == cfg["devices"], "different GPU set than qualification; no coverage")
                if not result["found_hex"]: break
                value = bytes.fromhex(result["found_hex"][0])
                index = gpu.hit_index(wordlist, value, job["count"])
                require(index < to_check, "hit outside evaluated prefix")
                hit_proof = confirm(cfg["header"], value, native=cfg["native"]["path"], qemu=cfg["qemu"]["path"])
                self.preserve_discovery(job, index, value, hit_proof)
                hit = value; to_check = index
                if index == 0: break
                # GPU order is parallel: prove the earlier consecutive prefix
                # separately before assigning a rank-based hit receipt.
            k = to_check if hit is not None else job["count"]
            ack = {"schema": "checked-prefix-v1", "job": job["id"], "plan": job["plan"],
                   "target": self.context["target"], "submitted": job["count"], "negative_prefix": k,
                   "status": "hit" if hit is not None else "negative"}
            if hit is not None: ack["hit_hex"] = hit.hex()
            proof = {"policy": gpu.POLICY, "delivery": delivery, "attempts": attempts, "confirmation": hit_proof}
            confirmation = {"schema": "confirmation-v1", "target": self.context["target"], "confirmed": True} if hit is not None else None
        # The temporary candidate file is gone BEFORE a durable completion.
        self.verify_runtime()
        if fault == "before_commit": raise RuntimeError("injected loss after GPU result before commit")
        self.append({"kind": "gpu-complete", "ack": ack, "confirmation": confirmation, "proof": proof})
        if fault == "after_commit": raise RuntimeError("injected loss after durable GPU completion")
        return {"status": ack["status"], "job": job, "committed_negative": k, "hit_hex": ack.get("hit_hex"),
                "negative_policy": gpu.POLICY, "seconds": time.monotonic()-began, "temporary_wordlist_retained": False}

    def status(self):
        return {**super().status(), "checker_policy": gpu.POLICY, "distributed": False,
                "gpu_qualification": self.configuration()["qualification_sha256"]}


class GpuCampaign(GpuMixin, history.HistoryCampaign):
    pass


class GpuControlCampaign(GpuMixin, CheckpointCampaign):
    """Tests and generated crypto targets ONLY; never creates real base exclusions."""
    pass


def create(directory, plan, target, header, evidence, qualification, *, accept_policy=False, evidence_policy=None):
    if evidence_policy is None:
        require(accept_policy is True, "explicit --accept-policy or --accept-legacy-entropy-evidence required; not digest-equivalent")
        evidence_policy = coverage.LEGACY_POLICY
    require(coverage.policies(evidence_policy) <= {coverage.LEGACY_POLICY, gpu.POLICY},
            "unsupported GPU history verifier policy")
    cfg = checker_config(qualification, header, target)
    checker = [cfg["runtime"]["executable"], gpu.MARKER, canonical(cfg).decode()]
    confirmer = [cfg["native"]["path"], "confirm-qemu", cfg["qemu"]["path"], cfg["qemu"]["sha256"]]
    return GpuCampaign.create(directory, plan, target, checker, confirmer, history=evidence,
        history_kind="coverage", evidence_policy=evidence_policy, core=CORE)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    commands = p.add_subparsers(dest="command", required=True)
    q = commands.add_parser("qualify")
    q.add_argument("directory", type=Path); q.add_argument("--hashcat", required=True, type=Path)
    q.add_argument("--hashcat-root", required=True, type=Path); q.add_argument("--devices", default="all")
    q.add_argument("--timeout", type=int, default=180)
    c = commands.add_parser("create")
    for name in ("directory", "plan", "target", "header", "evidence", "qualification"): c.add_argument(name, type=Path)
    c.add_argument("--accept-legacy-entropy-evidence", action="store_true")
    c.add_argument("--accept-policy", help="exact verifier policy from a migrated coverage.json")
    for verb in ("run", "status", "audit", "checkpoint", "revise", "preview"):
        x = commands.add_parser(verb); x.add_argument("directory", type=Path)
        if verb == "run":
            x.add_argument("--count", type=int, default=1_000_000); x.add_argument("--batches", type=int, default=1)
            x.add_argument("--timeout", type=int, default=600)
        if verb == "revise": x.add_argument("plan", type=Path)
    a = p.parse_args()
    if a.command == "qualify":
        result = qualify(a.directory, executable=a.hashcat, runtime_root=a.hashcat_root, selected=a.devices, timeout=a.timeout)
    elif a.command == "create":
        with create(a.directory, a.plan, a.target, a.header, a.evidence, a.qualification,
                    accept_policy=a.accept_legacy_entropy_evidence, evidence_policy=a.accept_policy) as campaign: result = campaign.status()
    else:
        with GpuCampaign(a.directory) as campaign:
            if a.command == "run":
                require(0 < a.batches <= 100000, "explicit batch limit 1..100000 required")
                for _ in range(a.batches):
                    result = campaign.run_batch(a.count, timeout=a.timeout)
                    print(json.dumps(result, sort_keys=True), flush=True)
                    if result["status"] in ("hit", "plan_depleted"): break
                return
            elif a.command == "revise": result = campaign.revise(a.plan)
            else: result = getattr(campaign, a.command)()
    print(json.dumps(result, indent=2))


if __name__ == "__main__": main()
