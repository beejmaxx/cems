"""CPU tests of GPU orchestration, NOT GPU qualification or real coverage.

The mocked backend explicitly substitutes for Hashcat. Native generation,
checkpointing, subtraction, digest verification and QEMU still execute.
"""
from layout import source as source_path
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

import gpu_backend as gpu
import gpu_worker as worker
import luks1_fixture as fixture
from checkpoint_runner import parse
from model_workflow import CORE, inspect
from runner import canonical, sha

SPOOL = Path(os.environ.get("HEX_SPOOL", gpu.SPOOL)).resolve()
DEVICE = {"device_id": 1, "device_name": "TEST ONLY mock GPU", "device_type": "GPU"}


def status(count, *, hit=None):
    return {"status": 6 if hit else 5, "progress": [count, count], "rejected": 0,
            "recovered_hashes": [1 if hit else 0, 1], "devices": [DEVICE]}


def frames(values): return b"".join(bytes([len(v)])+v for v in values)


class GpuHandoffTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix="gpu-handoff-controls-")
        cls.base = Path(cls.temp.name)
        cls.qemu = Path(shutil.which("qemu-img") or "missing-qemu").resolve(strict=True)
        data, _ = fixture.image(fixture.default_profile(), payload=bytes(512))
        cls.header = cls.base/"SYNTHETIC-header.luks"; fixture.write_new(cls.header, data)
        cls.target = cls.base/"SYNTHETIC-target.hash"; fixture.write_new(cls.target, gpu.extract(data))

    @classmethod
    def tearDownClass(cls): cls.temp.cleanup()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="gpu-bridge-test-")
        self.addCleanup(self.tmp.cleanup); self.root = Path(self.tmp.name)
        self.calls = []; self.mode = "normal"
        (self.root/"modules").mkdir(); (self.root/"OpenCL").mkdir()
        (self.root/"modules/module_29511.so").write_bytes(b"TEST ONLY mock module")
        (self.root/"OpenCL/m14611-pure.cl").write_bytes(b"TEST ONLY mock kernel")

    def mock_hashcat(self, identity, target, wordlist, count, directory, **kw):
        values = [bytes.fromhex(v.decode().strip()) for v in Path(wordlist).read_bytes().splitlines()][:count]
        self.assertEqual(len(values), count); self.calls.append(values)
        if self.mode == "timeout": raise RuntimeError("injected timeout")
        if self.mode == "false-hit": hit = values[0]
        else: hit = fixture.PASSWORD if fixture.PASSWORD in values else None
        result = gpu.finish_status(canonical(status(count, hit=hit)), 0 if hit else 1, count, [hit] if hit else [])
        return dict(result, seconds=0.01)

    def make_campaign(self, values, *, name="campaign"):
        cfg_path = self.root/(name+"-qualification.json"); fixture.write_new(cfg_path, b'{"TEST_ONLY":true}\n')
        cfg = {"schema": gpu.MARKER, "policy": gpu.POLICY,
               "qualification": str(cfg_path), "qualification_sha256": sha(cfg_path),
               "header": str(self.header), **gpu.bind_target(self.header, self.target),
               "runtime": {"executable": str(worker.NATIVE), "sha256": sha(worker.NATIVE), "runtime_tree": gpu.runtime_tree(self.root), "root": str(self.root)},
               "selected": "1", "devices": [DEVICE],
               "native": {"path": str(worker.NATIVE), "sha256": sha(worker.NATIVE)},
               "spool": {"path": str(SPOOL), "sha256": sha(SPOOL)},
               "qemu": {"path": str(self.qemu), "sha256": sha(self.qemu)},
               "source_hashes": {p.name: sha(p) for p in (Path(worker.__file__), Path(gpu.__file__), source_path("hex_spool.cpp"))}}
        d = self.root/(name+"-model"); d.mkdir()
        plan = worker.controlled_plan(d, values)
        return worker.GpuControlCampaign.create(self.root/name, plan, self.target,
            [str(worker.NATIVE), gpu.MARKER, canonical(cfg).decode()],
            [str(worker.NATIVE), "confirm-qemu", str(self.qemu), sha(self.qemu)], core=CORE)

    def test_extract_reproduces_upstream_format_and_detects_mismatch(self):
        binding = gpu.bind_target(self.header, self.target)
        self.assertEqual(binding["target_sha256"], sha(self.target))
        bad = self.root/"bad.hash"; fixture.write_new(bad, self.target.read_bytes()+b"\n")
        with self.assertRaisesRegex(RuntimeError, "exact checking extract"): gpu.bind_target(self.header, bad)

    def test_gpu_coverage_migrates_without_running_checker_and_stays_subtracted(self):
        from migrations.export import export_campaign, audit
        import coverage_snapshot as coverage
        values = [b"wrong-a", b"wrong-b", b"wrong-c"]
        with patch.object(gpu, "hashcat_run", side_effect=self.mock_hashcat):
            with self.make_campaign(values) as c:
                c.run_batch(2)
                checker, confirmer = c.context["checker"]["argv"], c.context["confirmer"]["argv"]
            before = len(self.calls)
            snapshot = self.root/"portable"
            doc = export_campaign(self.root/"campaign", snapshot)
            self.assertEqual(doc["policy"], gpu.POLICY)
            self.assertEqual(doc["completed"]["count"], 2)
            self.assertTrue(audit(snapshot)["verified"])
            self.assertEqual(len(self.calls), before)
            with worker.GpuCampaign.create(self.root/"resumed", self.root/"campaign-model/control.plan",
                    self.target, checker, confirmer, history=snapshot, history_kind="coverage",
                    evidence_policy=gpu.POLICY, core=CORE) as c:
                self.assertEqual(int(c.info(c.state["plan"])["candidates"]), 1)
                c.run_batch(1)
                self.assertTrue(c.audit()["verified"])
            self.assertEqual(self.calls[-1], [b"wrong-c"])
            mixed = coverage.combine_policies(coverage.LEGACY_POLICY, gpu.POLICY)
            self.assertEqual(coverage.policies(mixed), {coverage.LEGACY_POLICY, gpu.POLICY})

    def test_spool_metacharacters_lengths_and_chunk_boundaries(self):
        values = [b"a", b" ", b"$HEX[4142]", b"quote'\"slash\\:bang!", b"z"*64]*5000
        r = subprocess.run([str(SPOOL), str(len(values))], input=frames(values), capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertEqual(r.stdout, b"".join(v.hex().encode()+b"\n" for v in values))
        self.assertEqual(parse(r.stderr)["frames"], len(values))

    def test_spool_refuses_unsupported_and_truncated_never_as_negative(self):
        for data in (b"", b"\x00", b"\x41"+b"a"*65, b"\x01\x00", b"\x01\xff", b"\x01\n", b"\x02a", frames([b"a", b"b"])):
            r = subprocess.run([str(SPOOL), "1"], input=data, capture_output=True, timeout=5)
            self.assertNotEqual(r.returncode, 0)

    def test_exhaustion_requires_full_progress_zero_rejects_and_exit_status(self):
        good = status(7); self.assertEqual(gpu.finish_status(canonical(good), 1, 7, [])["status"], 5)
        bads = [dict(good, status=3), dict(good, rejected=1), dict(good, progress=[6,7]),
                dict(good, progress=[7,8]), dict(good, recovered_hashes=[1,1]),
                dict(good, devices=[]), dict(good, rejected=False)]
        for doc in bads:
            with self.assertRaises(RuntimeError): gpu.finish_status(canonical(doc), 1, 7, [])
        for code in (0,2,3,-9):
            with self.assertRaises(RuntimeError): gpu.finish_status(canonical(good), code, 7, [])
        with self.assertRaises(RuntimeError): gpu.finish_status(b"100% Exhausted", 1, 7, [])

    def test_final_not_intermediate_status_and_hit_output_is_required(self):
        good = canonical(status(3))
        with self.assertRaises(RuntimeError): gpu.finish_status(good+b"\n"+canonical(dict(status(3),status=7)), 1, 3, [])
        with self.assertRaises(RuntimeError): gpu.finish_status(canonical(status(3,hit=b"x")), 0, 3, [])

    def test_native_batch_negative_resume_revision_and_audit(self):
        with patch.object(gpu, "hashcat_run", side_effect=self.mock_hashcat):
            with self.make_campaign([b"old", b"unchecked", b"later"]) as c:
                result = c.run_batch(1); self.assertEqual(result["committed_negative"], 1)
                c.checkpoint()
            with worker.GpuControlCampaign(self.root/"campaign") as c:
                d = self.root/"revision"; d.mkdir()
                revised = worker.controlled_plan(d, [b"new", b"old", b"unchecked", b"later"])
                c.revise(revised)
                c.run_batch(2); c.checkpoint(); c.audit()
                self.assertEqual(c.status()["checked_negative"], 3)
            self.assertEqual(self.calls, [[b"old"], [b"new", b"unchecked"]])
            self.assertFalse(list((self.root/"campaign").glob("gpu-batch-*")))

    def test_confirmed_parallel_hit_proves_negative_prefix_separately(self):
        with patch.object(gpu, "hashcat_run", side_effect=self.mock_hashcat):
            with self.make_campaign([b"wrong-a", b"wrong-b", fixture.PASSWORD, b"tail"]) as c:
                result = c.run_batch(4)
                self.assertEqual((result["status"], result["committed_negative"]), ("hit",2))
                c.checkpoint(); c.audit()
            with worker.GpuControlCampaign(self.root/"campaign") as c:
                self.assertEqual(c.status()["hit"]["hex"], fixture.PASSWORD.hex())
            self.assertEqual(self.calls[-1], [b"wrong-a", b"wrong-b"])

    def test_first_hit_has_zero_credited_negatives(self):
        with patch.object(gpu, "hashcat_run", side_effect=self.mock_hashcat):
            with self.make_campaign([fixture.PASSWORD, b"tail"]) as c:
                result = c.run_batch(2); self.assertEqual(result["committed_negative"], 0)
                self.assertEqual(len(self.calls), 1); c.audit()

    def test_confirmed_discovery_survives_failed_prefix_accounting(self):
        def backend(*args, **kwargs):
            if self.calls: raise RuntimeError("injected prefix interruption")
            return self.mock_hashcat(*args, **kwargs)
        with patch.object(gpu, "hashcat_run", side_effect=backend):
            with self.make_campaign([b"before", fixture.PASSWORD, b"tail"]) as c:
                with self.assertRaisesRegex(RuntimeError, "prefix interruption"): c.run_batch(3)
                self.assertEqual(c.state["checked_negative"], 0)
                self.assertIsNone(c.state["hit"])
                files = list((c.root/"discoveries").glob("*.json"))
                self.assertEqual(len(files), 1)
                found = json.loads(files[0].read_text())
                self.assertEqual(found["hit_hex"], fixture.PASSWORD.hex())
                self.assertEqual(found["negative_coverage_claimed"], 0)
                self.assertTrue(found["confirmation"]["native_digest"])
                self.assertEqual(files[0].stat().st_mode & 0o777, 0o600)

    def test_timeout_retries_unchanged_job_and_ack_loss_does_not_skip(self):
        with patch.object(gpu, "hashcat_run", side_effect=self.mock_hashcat):
            with self.make_campaign([b"one", b"two", b"three"]) as c:
                self.mode = "timeout"
                with self.assertRaisesRegex(RuntimeError, "timeout"): c.run_batch(2)
                pending = copy.deepcopy(c.state["pending"])
                self.assertEqual(c.state["checked_negative"], 0)
            self.mode = "normal"
            with worker.GpuControlCampaign(self.root/"campaign") as c:
                with self.assertRaisesRegex(RuntimeError, "before commit"): c.run_batch(1, fault="before_commit")
                self.assertEqual(c.state["pending"], pending)
            with worker.GpuControlCampaign(self.root/"campaign") as c:
                c.run_batch(1); self.assertEqual(c.state["checked_negative"], 2)
                c.run_batch(1); self.assertEqual(c.state["checked_negative"], 3)
            self.assertEqual(self.calls, [[b"one", b"two"]]*3+[[b"three"]])

    def test_post_commit_loss_does_not_repeat_and_false_hit_is_uncredited(self):
        with patch.object(gpu, "hashcat_run", side_effect=self.mock_hashcat):
            with self.make_campaign([b"one", b"two"]) as c:
                with self.assertRaisesRegex(RuntimeError, "durable"): c.run_batch(1, fault="after_commit")
            with worker.GpuControlCampaign(self.root/"campaign") as c:
                self.mode = "false-hit"
                with self.assertRaisesRegex(RuntimeError, "master-key digest"): c.run_batch(1)
                self.assertEqual(c.state["checked_negative"], 1)
            self.assertEqual(self.calls, [[b"one"], [b"two"]])

    def test_source_mutation_invalidates_runtime_and_bare_completion_is_rejected(self):
        with self.make_campaign([b"one"]) as c:
            with self.assertRaisesRegex(RuntimeError, "terminal-checker"): c.append({"kind":"complete"})
            cfg = c.configuration(); Path(cfg["qualification"]).write_bytes(b"changed")
            with self.assertRaisesRegex(RuntimeError, "qualification changed"): c.verify_runtime()

    def test_no_qualification_or_explicit_policy_means_no_real_campaign(self):
        with self.assertRaisesRegex(RuntimeError, "explicit"):
            worker.create(self.root/"no-campaign", "missing", self.target, self.header, "missing", "missing")
        self.assertFalse((self.root/"no-campaign").exists())

    def test_real_subprocess_bridge_and_timeout_are_fail_closed(self):
        # Deliberately NOT a qualifying Hashcat: --version never claims 7.1.2.
        program = self.root/"TEST_ONLY_fake_checker"
        program.write_text('''#!/usr/bin/env python3
import json, pathlib, sys, time
a = sys.argv[1:]
if '--version' in a: print('TEST_ONLY_NOT_HASHCAT'); sys.exit(0)
spec = json.loads(pathlib.Path(a[-2]).read_text())
if spec.get('sleep'): time.sleep(60)
values = pathlib.Path(a[-1]).read_text().splitlines()
n = int(a[a.index('--limit')+1]) if '--limit' in a else len(values)
hit = spec.get('hit')
if hit: pathlib.Path(a[a.index('--outfile')+1]).write_text(hit+'\\n')
print(json.dumps({'status': spec.get('status', 6 if hit else 5), 'progress': [n,n],
  'rejected': spec.get('rejected',0), 'recovered_hashes': [1 if hit else 0,1],
  'devices': [{'device_id':1,'device_name':'TEST ONLY mock GPU','device_type':'GPU'}]}))
sys.exit(0 if hit else 1)
''')
        program.chmod(0o700)
        identity = {"executable": str(program), "sha256": sha(program), "root": str(self.root), "runtime_tree": gpu.runtime_tree(self.root)}
        target, words = self.root/"TEST_ONLY.json", self.root/"words.hex"
        words.write_bytes(b"61\n62\n63\n")
        target.write_text("{}")
        result = gpu.hashcat_run(identity, target, words, 3, self.root/"negative", timeout=5)
        self.assertEqual(result["status"], 5)
        target.write_text(json.dumps({"hit": "62"}))
        result = gpu.hashcat_run(identity, target, words, 3, self.root/"hit", timeout=5)
        self.assertEqual(result["found_hex"], ["62"])
        target.write_text(json.dumps({"rejected": 1}))
        with self.assertRaisesRegex(RuntimeError, "rejected"): gpu.hashcat_run(identity, target, words, 3, self.root/"reject", timeout=5)
        target.write_text(json.dumps({"sleep": True}))
        with self.assertRaisesRegex(RuntimeError, "timeout"): gpu.hashcat_run(identity, target, words, 3, self.root/"timeout", timeout=1)


if __name__ == "__main__": unittest.main()
