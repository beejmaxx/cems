"""Durable lifecycle tests; all predicates/coverage here are synthetic."""
from layout import BUILD
from fractions import Fraction as Q
import json
import os
from pathlib import Path
import random
import shutil
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fixtures import explicit_graph, write_source
from runner import Campaign, CAP, disk_guard, sha
from test_core import BIN, expected, rows, run

from layout import LAB as HERE
CHECKER = Path(os.environ.get("CHECKER_BINARY", BUILD / "synthetic-checker")).resolve()


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="runner-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.serial = 0

    def plan(self, d, weights=None):
        self.serial += 1
        source = self.root / f"model-{self.serial}.swg"
        plan = self.root / f"model-{self.serial}.plan"
        write_source(source, {n: explicit_graph(values) for n, values in d.items()}, weights or dict.fromkeys(d, 1))
        run("compile", source, plan)
        return plan

    def create(self, plan, *, limit="all", mode="ok", secret=b"not-present", name="campaign", checker=None):
        target = self.root / (name + ".target")
        target.write_bytes(secret)
        checker = str(checker or CHECKER)
        return Campaign.create(self.root / name, plan, target, [checker, "check", str(limit), mode],
                               [checker, "confirm"], core=BIN)

    def test_partial_prefix_then_restart_does_not_credit_tail(self):
        plan = self.plan({"A": {bytes([c]): 1 for c in range(97, 107)}})
        with self.create(plan, limit=2) as c:
            result = c.run_batch(7)
            self.assertEqual((result["generated_and_delivered"], result["committed_negative"], result["uncredited_tail"]), (7, 2, 5))
            self.assertEqual(c.status()["cursor"], 2)
        # Fresh process, not just another object, audits receipts and continues.
        program = "from runner import Campaign; import json,sys; c=Campaign(sys.argv[1]); print(json.dumps(c.run_batch(7))); c.close()"
        answer = subprocess.run([os.sys.executable, "-B", "-c", program, str(self.root / "campaign")],
                                cwd=HERE, capture_output=True, check=True, timeout=20)
        self.assertEqual(json.loads(answer.stdout)["job"]["start"], 2)
        with Campaign(self.root / "campaign") as c:
            while c.status()["available_in_plan"]:
                c.run_batch(7)
            self.assertEqual(c.status()["checked_negative"], 10)
            self.assertEqual(c.status()["history_intervals"], 1)
            self.assertEqual(c.run_batch(7)["status"], "plan_depleted")

    def test_lost_ack_retries_same_job_but_lost_reply_after_commit_does_not(self):
        plan = self.plan({"A": {bytes([c]): 1 for c in range(10)}})
        with self.create(plan) as c:
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                c.run_batch(3, fault="before_commit")
            pending = c.status()["pending"]
            self.assertEqual(c.status()["checked_negative"], 0)
        with Campaign(self.root / "campaign") as c:
            result = c.run_batch(1)  # immutable pending batch wins over a new size
            self.assertEqual(result["job"], pending)
            with self.assertRaisesRegex(RuntimeError, "durable commit"):
                c.run_batch(2, fault="after_commit")
        with Campaign(self.root / "campaign") as c:
            self.assertEqual(c.status()["cursor"], 5)
            self.assertEqual(c.run_batch(2)["job"]["start"], 5)
            self.assertEqual(c.status()["checked_negative"], 7)

    def test_abrupt_controller_exit_reopens_wal_before_and_after_commit(self):
        plan = self.plan({"A": {bytes([c]): 1 for c in range(10)}})
        for fault, expected_cursor in (("before_commit", 0), ("after_commit", 3)):
            with self.create(plan, name=fault):
                pass
            program = ("from runner import Campaign; import os,sys; c=Campaign(sys.argv[1]); "
                       "\ntry: c.run_batch(3, fault=sys.argv[2])"
                       "\nexcept RuntimeError: os._exit(86)")
            p = subprocess.run([os.sys.executable, "-B", "-c", program, str(self.root / fault), fault],
                               cwd=HERE, capture_output=True, timeout=20)
            self.assertEqual(p.returncode, 86)
            with Campaign(self.root / fault) as c:
                self.assertEqual(c.status()["cursor"], expected_cursor)
                self.assertEqual(c.run_batch(3)["job"]["start"], expected_cursor)

    def test_revision_reweights_old_unchecked_and_adds_overlap(self):
        a = {"A": {b"a": 8, b"x": 6, b"b": 4, b"c": 2}}
        b = {"A": {b"a": 1, b"x": 1, b"b": 4, b"c": 8}, "new": {b"a": 4, b"c": 6, b"y": 5}}
        first, second = self.plan(a), self.plan(b, {"A": 1, "new": 3})
        with self.create(first, limit=1) as c:
            result = c.run_batch(4)
            checked = {v for v, _ in rows(first, 0, result["committed_negative"])}
            self.assertEqual(checked, {b"a"})
            c.revise(second)
            active = c.blob(c.status()["plan"])
            want = expected(b, {"A": 1, "new": 3}, checked)
            self.assertEqual(rows(active, 0, len(want)), want)
            self.assertEqual(want[0][0], b"c")  # old low-ranked, unchecked promoted
            self.assertIn(b"x", {v for v, _ in want})  # submitted but NOT acknowledged
            while c.status()["available_in_plan"]:
                j = c.run_batch(3)
                got = rows(active, j["job"]["start"], j["committed_negative"])
                self.assertTrue(checked.isdisjoint(v for v, _ in got))
                checked.update(v for v, _ in got)
            self.assertEqual(checked, {b"a", b"b", b"c", b"x", b"y"})
        with Campaign(self.root / "campaign") as c:
            c.revise(first)  # old model reintroduced: not a license to repeat it
            self.assertEqual(c.status()["available_in_plan"], 0)

    def test_unacknowledged_old_job_can_be_retired_but_not_counted(self):
        a = self.plan({"A": {b"a": 1, b"b": 1, b"c": 1}})
        b = self.plan({"B": {b"b": 5, b"c": 3, b"d": 2, b"a": 1}})
        with self.create(a) as c:
            c.run_batch(1)
            with self.assertRaises(RuntimeError):
                c.run_batch(2, fault="before_commit")
            c.revise(b)
            self.assertIsNone(c.status()["pending"])
            self.assertEqual([v for v, _ in rows(c.blob(c.state["plan"]), 0, 3)], [b"b", b"c", b"d"])

    def test_new_construct_finds_secret_and_independently_confirms_it(self):
        a = self.plan({"A": {b"a": 5, b"b": 4, b"c": 3}})
        b = self.plan({"B": {b"b": 5, b"a": 4, b"\0\n\xff": 3, b"c": 2, b"after": 1}})
        with self.create(a, secret=b"\0\n\xff") as c:
            c.run_batch(1)
            c.revise(b)
            answer = c.run_batch(10)
            self.assertEqual(answer["status"], "hit")
            self.assertEqual(answer["committed_negative"], 1)
            self.assertEqual(answer["uncredited_tail"], 2)
            self.assertEqual(c.status()["checked_negative"], 2)
            self.assertEqual(c.status()["hit"]["hex"], "000aff")
        with Campaign(self.root / "campaign") as c:
            with self.assertRaisesRegex(RuntimeError, "confirmed hit"):
                c.run_batch(2)
            with self.assertRaisesRegex(RuntimeError, "confirmed hit"):
                c.revise(a)

    def test_empty_secret_and_byte_exact_predicate(self):
        p = self.plan({"A": {b"": 2, b"x": 1}})
        with self.create(p, secret=b"") as c:
            self.assertEqual(c.run_batch(2)["status"], "hit")
            self.assertEqual(c.status()["hit"]["hex"], "")
            self.assertEqual(c.status()["checked_negative"], 0)

    def test_malformed_wrong_identity_excess_and_unconfirmed_never_become_coverage(self):
        plan = self.plan({"A": {b"a": 2, b"b": 1}})
        for mode in ("exit", "malformed", "wrong-job", "overclaim", "false-hit", "lie-hit", "spam", "block"):
            with self.subTest(mode=mode):
                with self.create(plan, mode=mode, name=mode) as c:
                    with self.assertRaises((RuntimeError, ValueError, subprocess.TimeoutExpired)):
                        c.run_batch(2, timeout=0.5 if mode == "block" else 10)
                    self.assertEqual(c.status()["checked_negative"], 0)
                    self.assertEqual(c.status()["receipts"], 0)
                with Campaign(self.root / mode) as c:
                    self.assertIsNotNone(c.status()["pending"])
                    self.assertEqual(c.status()["cursor"], 0)

    def test_exclusive_owner_missing_history_and_storage_bound(self):
        p = self.plan({"A": {b"x": 1}})
        with self.create(p) as c:
            with self.assertRaisesRegex(RuntimeError, "already has an owner"):
                Campaign(c.root)
            with self.assertRaisesRegex(RuntimeError, "storage cap"):
                disk_guard(c.root, CAP)
            with patch("runner.disk_guard", side_effect=RuntimeError("free disk reserve")):
                with self.assertRaisesRegex(RuntimeError, "disk reserve"):
                    c.run_batch(1)
            self.assertIsNone(c.status()["pending"])
        with self.assertRaisesRegex(RuntimeError, "campaign missing"):
            Campaign(self.root / "absent")
        (self.root / "empty").mkdir()
        with self.assertRaisesRegex(RuntimeError, "history database missing"):
            Campaign(self.root / "empty")

    def test_artifact_mutation_and_receipt_damage_fail_closed(self):
        p = self.plan({"A": {b"a": 1, b"b": 1}})
        for artifact in ("plan", "target", "receipt"):
            with self.create(p, name=artifact) as c:
                c.run_batch(1)
                if artifact == "plan":
                    path = c.blob(c.state["plan"])
                elif artifact == "target":
                    path = c.blob(c.context["target"], "targets")
            if artifact == "receipt":
                with sqlite3.connect(self.root / artifact / "history.sqlite") as db:
                    db.execute("UPDATE events SET body=? WHERE seq=3", (b"{}",))
            else:
                path.write_bytes(b"damaged")
            with self.assertRaises((RuntimeError, ValueError)):
                Campaign(self.root / artifact)

    def test_changed_executable_rejected_no_silent_upgrade(self):
        checker = self.root / "checker-copy"
        shutil.copy2(CHECKER, checker)
        p = self.plan({"A": {b"a": 1}})
        with self.create(p, checker=checker):
            pass
        with checker.open("ab") as f:
            f.write(b"changed-build")
        with self.assertRaisesRegex(RuntimeError, "changed; explicit migration"):
            Campaign(self.root / "campaign")

    def test_many_revisions_against_independent_exact_set_oracle(self):
        rng = random.Random(93219)
        universe = [bytes([c]) for c in range(20)]
        done = set()
        for revision in range(12):
            d = {str(i): {v: rng.randint(1, 9) for v in rng.sample(universe, 12)} for i in range(2)}
            weights = {"0": rng.randint(1, 6), "1": rng.randint(1, 6)}
            plan = self.plan(d, weights)
            c = self.create(plan) if revision == 0 else Campaign(self.root / "campaign")
            with c:
                if revision:
                    c.revise(plan)
                wanted = expected(d, weights, done)
                active = c.blob(c.state["plan"])
                self.assertEqual(rows(active, 0, len(wanted)), wanted)
                result = c.run_batch(2)
                if wanted:
                    tested = {v for v, _ in wanted[:result["committed_negative"]]}
                    self.assertTrue(done.isdisjoint(tested))
                    done.update(tested)
                self.assertEqual(c.status()["checked_negative"], len(done))


if __name__ == "__main__":
    unittest.main()
