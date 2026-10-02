"""Real cryptography, synthetic targets. No access to the personal header.

Independent checks use QEMU and Python/PyCryptodome; candidate processing uses
the native C++ adapter and the original durable worker, without modifications.
"""
from layout import BUILD
import copy
import hashlib
import json
import os
from pathlib import Path
import shutil
import struct
import subprocess
import tempfile
import unittest

from checkpoint_runner import CheckpointCampaign
from fixtures import explicit_graph, write_source
import luks1_fixture as fixture
from luks1_worker import commands
from runner import HERE, sha
from test_core import BIN

CHECKER = Path(os.environ.get("LUKS1_CHECKER", BUILD / "luks1-checker")).resolve()
QEMU = Path(shutil.which("qemu-img") or "/missing-qemu-img").resolve()
PASS = fixture.PASSWORD


def frames(values):
    return b"".join(bytes([len(v)])+v for v in values)


class LuksTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        if not CHECKER.is_file() or not QEMU.is_file():
            raise RuntimeError("build/luks1-checker and qemu-img are REQUIRED; qualification may not silently skip")
        cls.control_tmp = tempfile.TemporaryDirectory(prefix="luks-real-crypto-tests-")
        cls.root = Path(cls.control_tmp.name)
        cls.profile = fixture.default_profile()
        # Matched computational parameters, independently generated key/salts.
        cls.data, cls.plain = fixture.image(cls.profile, payload=bytes(range(256))*2)
        cls.target = cls.root/"control.luks"
        fixture.write_new(cls.target, cls.data)

    @classmethod
    def tearDownClass(cls): cls.control_tmp.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="luks-adapter-case-")
        self.addCleanup(self.temp.cleanup)
        self.work = Path(self.temp.name)

    def call(self, values, *, target=None, slot="all", limit="all", raw=None, digest=None):
        target = target or self.target
        return subprocess.run([str(CHECKER), "check", "control", slot, str(limit), str(target),
            "a"*32, "b"*64, digest or sha(target), str(len(values))],
            input=frames(values) if raw is None else raw, capture_output=True, timeout=30)

    def ack(self, values, **kwargs):
        r = self.call(values, **kwargs)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        return json.loads(r.stdout)

    def write(self, data, name="mutated.luks"):
        p = self.work/name; fixture.write_new(p, data); return p

    def plan(self, masses, name):
        source, plan = self.work/f"{name}.swg", self.work/f"{name}.plan"
        write_source(source, {"model": explicit_graph(masses)}, {"model": 1})
        subprocess.run([str(BIN), "compile", str(source), str(plan)], check=True, capture_output=True, timeout=100)
        return plan

    def campaign(self, plan, name="campaign", limit="all", confirmer=None):
        checker, normal = commands(CHECKER, QEMU, limit=limit)
        return CheckpointCampaign.create(self.work/name, plan, self.target, checker, confirmer or normal, core=BIN)

    def test_exact_profile_and_independent_qemu_positive_negative(self):
        self.assertEqual(fixture.profile(self.data), self.profile)
        p = json.loads(subprocess.check_output([str(CHECKER), "inspect", str(self.target)]))
        self.assertEqual(p["active_slots"][0], {"slot": 0, "iterations": 105474, "offset_sector": 8, "stripes": 4000})
        self.assertEqual(p["digest_iterations"], 34000)
        self.assertIsNotNone(fixture.unwrap(self.data, PASS))
        self.assertIsNone(fixture.unwrap(self.data, b"Coffee.9.Morning"))
        q = fixture.qemu_read(self.target, PASS, QEMU)
        self.assertTrue(q["accepted"])
        self.assertEqual(q["plaintext_sha256"], hashlib.sha256(self.plain).hexdigest())
        self.assertFalse(fixture.qemu_read(self.target, PASS+b"!", QEMU)["accepted"])

    def test_correct_first_middle_last_and_wrong_only(self):
        for position in range(3):
            values = [b"wrong-1", b"wrong-2"]; values.insert(position, PASS)
            a = self.ack(values)
            self.assertEqual((a["status"], a["negative_prefix"], a["hit_hex"]), ("hit", position, PASS.hex()))
        a = self.ack([b"wrong-1", b"wrong-2", PASS+b"!"])
        self.assertEqual((a["status"], a["negative_prefix"]), ("negative", 3))

    def test_native_qemu_confirmer_pinned_binary_and_wrong_password(self):
        argv = [str(CHECKER), "confirm-qemu", str(QEMU), sha(QEMU), str(self.target), sha(self.target)]
        r = subprocess.run(argv, input=frames([PASS]), capture_output=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr.decode())
        self.assertTrue(json.loads(r.stdout)["confirmed"])
        for changed in (argv[:3]+["0"*64]+argv[4:], argv[:-1]+["0"*64]):
            bad = subprocess.run(changed, input=frames([PASS]), capture_output=True, timeout=30)
            self.assertNotEqual(bad.returncode, 0); self.assertEqual(bad.stdout, b"")
        bad = subprocess.run(argv, input=frames([b"wrong"]), capture_output=True, timeout=30)
        self.assertNotEqual(bad.returncode, 0); self.assertEqual(bad.stdout, b"")

    def test_payload_entropy_and_header_only_do_not_change_digest_acceptance(self):
        offset = self.profile["payload_sector"]*512
        for i, tail in enumerate((b"", bytes(512), b"\xff"*512, os.urandom(512))):
            path = self.write(self.data[:offset]+tail, f"case-{i}.luks")
            self.assertEqual(self.ack([PASS], target=path)["status"], "hit")

    def test_partial_drains_tail_but_does_not_credit_it(self):
        a = self.ack([b"wrong", PASS, b"tail"], limit=1)
        self.assertEqual((a["status"], a["negative_prefix"]), ("partial", 1))
        self.assertNotIn("hit_hex", a)
        self.assertEqual(self.ack([PASS], limit=0)["negative_prefix"], 0)

    def test_malformed_frames_never_issue_receipt_even_after_hit(self):
        for raw in (b"", frames([PASS])[:-1], frames([PASS])+b"\x00", b"\x81"+b"x"*129,
                    frames([PASS])+b"\x08broken"):
            r = self.call([PASS], raw=raw)
            self.assertNotEqual(r.returncode, 0); self.assertEqual(r.stdout, b"")

    def test_bad_targets_profiles_parameters_and_identity_fail_closed(self):
        edits = [(6, b"\0\2"), (8, b"bad"), (40, b"xts"), (72, b"sha256\0"),
                 (108, struct.pack(">I", 16)), (164, bytes(4)), (212, bytes(4)),
                 (208, struct.pack(">I", 123)), (248, struct.pack(">I", 1)),
                 (252, struct.pack(">I", 0)), (252, struct.pack(">I", 0xffffffff))]
        for i, (where, value) in enumerate(edits):
            d = bytearray(self.data); d[where:where+len(value)] = value
            r = self.call([PASS], target=self.write(d, f"bad-{i}.luks"))
            self.assertNotEqual(r.returncode, 0); self.assertEqual(r.stdout, b"")
        for i, d in enumerate((self.data[:208], self.data[:5000])):
            self.assertNotEqual(self.call([PASS], target=self.write(d, f"short-{i}.luks")).returncode, 0)
        self.assertNotEqual(self.call([PASS], digest="0"*64).returncode, 0)
        self.assertNotEqual(self.call([PASS], slot="1").returncode, 0)

    def test_changed_digest_or_key_material_is_negative_not_success(self):
        for i, pos in enumerate((112, 8*512+100)):
            d = bytearray(self.data); d[pos] ^= 1
            a = self.ack([PASS], target=self.write(d, f"damaged-{i}.luks"))
            self.assertEqual((a["status"], a["negative_prefix"]), ("negative", 1))

    def test_binary_bytes_empty_max_length_and_partial_sector_af(self):
        p = copy.deepcopy(self.profile); p["digest_iterations"] = 7
        p["slots"][0].update(iterations=11, stripes=17)
        for i, password in enumerate((b"", b"\0\n\xff", b"x"*128, b"a\nb")):
            d, _ = fixture.image(p, password)
            path = self.write(d, f"binary-{i}.luks")
            self.assertIsNotNone(fixture.unwrap(d, password))
            a = self.ack([b"wrong", password], target=path)
            self.assertEqual((a["negative_prefix"], a["hit_hex"]), (1, password.hex()))
        argv = [str(CHECKER), "confirm-qemu", str(QEMU), sha(QEMU), str(self.target), sha(self.target)]
        r = subprocess.run(argv, input=frames([b"Coffee.8.Morning\0ignored"]), capture_output=True, timeout=30)
        self.assertNotEqual(r.returncode, 0); self.assertEqual(r.stdout, b"")

    def test_nonzero_active_slot_and_all_slots(self):
        p = copy.deepcopy(self.profile); p["digest_iterations"] = 11
        p["slots"][0]["active"] = False
        p["slots"][3].update(active=True, iterations=19, stripes=4000)
        d, _ = fixture.image(p); path = self.write(d)
        for slot in ("all", "3"):
            self.assertEqual(self.ack([PASS], target=path, slot=slot)["status"], "hit")
        self.assertNotEqual(self.call([PASS], target=path, slot="0").returncode, 0)
        self.assertTrue(fixture.qemu_read(path, PASS, QEMU)["accepted"])

    def test_independently_qemu_created_volume(self):
        secret, target = self.work/"secret", self.work/"qemu-created.luks"
        fixture.write_new(secret, PASS)
        options = "key-secret=s,cipher-alg=aes-256,cipher-mode=cbc,ivgen-alg=essiv,ivgen-hash-alg=sha256,hash-alg=sha1,iter-time=1"
        subprocess.run([str(QEMU), "create", "--object", f"secret,id=s,format=raw,file={secret}",
                        "-f", "luks", "-o", options, str(target), "1M"],
                       check=True, capture_output=True, timeout=30)
        self.assertEqual(self.ack([b"wrong", PASS], target=target)["hit_hex"], PASS.hex())
        self.assertIsNotNone(fixture.unwrap(target.read_bytes(), PASS))

    def test_all_slots_tries_later_slot_when_first_does_not_match(self):
        d = bytearray(self.data)
        # The same volume key is wrapped in slot 3. Slot 0's changed salt makes
        # this password wrong there, without corrupting the slot structure.
        d[352:400] = d[208:256]
        struct.pack_into(">I", d, 392, 776)
        d[776*512:776*512+128000] = d[8*512:8*512+128000]
        d[216] ^= 1
        path = self.write(d)
        self.assertEqual(self.ack([PASS], target=path, slot="0")["status"], "negative")
        for slot in ("all", "3"):
            self.assertEqual(self.ack([PASS], target=path, slot=slot)["status"], "hit")
        self.assertTrue(fixture.qemu_read(path, PASS, QEMU)["accepted"])

    def test_non_regular_symlink_and_overlapping_active_slot_rejected(self):
        link = self.work/"link.luks"; link.symlink_to(self.target)
        for path in (link, self.work, Path("/dev/null")):
            r = self.call([PASS], target=path, digest="a"*64)
            self.assertNotEqual(r.returncode, 0); self.assertEqual(r.stdout, b"")
        d = bytearray(self.data); d[256:304] = d[208:256]
        self.assertNotEqual(self.call([PASS], target=self.write(d)).returncode, 0)

    def test_worker_partial_restart_revise_and_independently_confirmed_hit(self):
        old = self.plan({b"first-wrong": 5, b"second-wrong": 3, b"old-tail": 1}, "old")
        new = self.plan({b"first-wrong": 20, b"new-wrong": 15, PASS: 10, b"new-tail": 1}, "new")
        with self.campaign(old, limit=1) as c:
            self.assertEqual(c.run_batch(3)["committed_negative"], 1)
            self.assertEqual(c.status()["checked_negative"], 1)
        with CheckpointCampaign(self.work/"campaign") as c:
            c.revise(new)
            self.assertEqual(c.status()["available_in_plan"], 3) # Completed overlap excluded.
            self.assertEqual(c.run_batch(3)["committed_negative"], 1)
        with CheckpointCampaign(self.work/"campaign") as c:
            r = c.run_batch(3)
            self.assertEqual((r["status"], r["confirmed_hits"], r["uncredited_tail"]), ("hit", 1, 1))
            self.assertEqual(c.status()["hit"]["hex"], PASS.hex())
            self.assertEqual(c.status()["checked_negative"], 2)
            c.checkpoint(); c.audit()
        with CheckpointCampaign(self.work/"campaign") as c:
            self.assertEqual(c.status()["hit"]["hex"], PASS.hex())
            with self.assertRaisesRegex(RuntimeError, "confirmed hit"): c.run_batch(1)

    def test_checker_timeout_and_confirmation_failure_are_not_coverage(self):
        plan = self.plan({b"wrong": 5, PASS: 4}, "model")
        with self.campaign(plan) as c:
            with self.assertRaises(subprocess.TimeoutExpired): c.run_batch(2, timeout=0.001)
            self.assertEqual(c.status()["checked_negative"], 0)
            self.assertIsNotNone(c.status()["pending"])
        with CheckpointCampaign(self.work/"campaign") as c:
            self.assertEqual(c.run_batch(2)["status"], "hit")
        _, confirmer = commands(CHECKER, QEMU); confirmer[3] = "0"*64
        with self.campaign(plan, name="bad-confirmation", confirmer=confirmer) as c:
            with self.assertRaises(RuntimeError): c.run_batch(2)
            self.assertEqual(c.status()["checked_negative"], 0)
            self.assertIsNone(c.status()["hit"])
            self.assertIsNotNone(c.status()["pending"])

    def test_lost_ack_replays_but_committed_prefix_does_not(self):
        plan = self.plan({b"wrong-1": 3, b"wrong-2": 2, PASS: 1}, "model")
        with self.campaign(plan) as c:
            with self.assertRaisesRegex(RuntimeError, "before commit"): c.run_batch(1, fault="before_commit")
            job = c.status()["pending"]
        with CheckpointCampaign(self.work/"campaign") as c:
            r = c.run_batch(1); self.assertEqual(r["job"], job)
            with self.assertRaisesRegex(RuntimeError, "durable commit"): c.run_batch(1, fault="after_commit")
        with CheckpointCampaign(self.work/"campaign") as c:
            self.assertEqual(c.status()["cursor"], 2)
            self.assertEqual(c.run_batch(1)["status"], "hit")


if __name__ == "__main__": unittest.main()
