"""Independent set oracles and storage fault tests. No real recovery coverage."""
import copy
import json
import os
from pathlib import Path
import random
import sqlite3
import subprocess
import unittest
from unittest.mock import patch

from checkpoint_runner import CheckpointCampaign, import_v1_copy, canonical, sha_bytes
from runner import Campaign as OriginalCampaign, sha
import test_runner as previous
from test_runner import HERE, CHECKER
from test_core import BIN, expected, rows


class CheckpointTests(unittest.TestCase):
    setUp = previous.RunnerTests.setUp
    plan = previous.RunnerTests.plan
    test_empty_secret_and_byte_exact_predicate = previous.RunnerTests.test_empty_secret_and_byte_exact_predicate

    def create(self, plan, *, limit="all", mode="ok", secret=b"not-present", name="campaign", checker=None,
               checkpoint_every=0, evidence_domain="checker-receipts"):
        target = self.root / (name + ".target")
        target.write_bytes(secret)
        checker = str(checker or CHECKER)
        return CheckpointCampaign.create(self.root / name, plan, target,
            [checker, "check", str(limit), mode], [checker, "confirm"], core=BIN,
            checkpoint_every=checkpoint_every, evidence_domain=evidence_domain)

    def test_checkpoint_reopen_reweight_and_exact_exclusion(self):
        a = {"A": {b"a": 8, b"x": 6, b"b": 4, b"c": 2}}
        b = {"A": {b"a": 1, b"x": 1, b"b": 4, b"c": 8}, "new": {b"a": 4, b"c": 6, b"y": 5}}
        first, second = self.plan(a), self.plan(b, {"A": 1, "new": 3})
        with self.create(first, limit=1) as c:
            c.run_batch(4)  # all generated, ONLY first checked
            before = copy.deepcopy(c.state)
            old_head = c.head
            metrics = c.checkpoint()
            self.assertEqual(c.db.execute("SELECT count(*) FROM tail").fetchone()[0], 0)
            self.assertEqual(c.state, before)
            self.assertEqual(c.head, old_head)
            self.assertEqual(metrics["compacted_events"], 3)
            self.assertTrue(c.audit()["verified"])
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertEqual(c.status()["startup_events_replayed"], 0)
            self.assertEqual(c.state, before)
            c.revise(second)
            want = expected(b, {"A": 1, "new": 3}, {b"a"})
            self.assertEqual(rows(c.blob(c.state["plan"]), 0, len(want)), want)
            self.assertEqual(want[0][0], b"c")
            c.run_batch(4)
            c.checkpoint()
            self.assertTrue(c.audit()["verified"])
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertEqual(c.state["checked_negative"], 2)
            self.assertEqual(c.state["cursor"], 1)

    def test_pending_and_partial_tail_survive_checkpoint(self):
        p = self.plan({"A": {bytes([i]): 1 for i in range(10)}})
        with self.create(p) as c:
            c.run_batch(2)
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                c.run_batch(3, fault="before_commit")
            pending = copy.deepcopy(c.state["pending"])
            c.checkpoint()
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertEqual(c.state["pending"], pending)
            result = c.run_batch(1)
            self.assertEqual(result["job"], pending)
            self.assertEqual(c.state["checked_negative"], 5)
            c.checkpoint()
            self.assertTrue(c.audit()["verified"])

    def test_checkpoint_keeps_confirmed_hit(self):
        p = self.plan({"A": {b"a": 3, b"\0\n\xff": 2, b"tail": 1}})
        with self.create(p, secret=b"\0\n\xff") as c:
            c.run_batch(3)
            before = copy.deepcopy(c.state)
            c.checkpoint()
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertEqual(c.state, before)
            self.assertEqual(c.state["checked_negative"], 1)
            with self.assertRaisesRegex(RuntimeError, "confirmed hit"):
                c.run_batch(1)
            self.assertTrue(c.audit()["verified"])

    def test_crash_boundaries_recover_exactly_or_fail_closed(self):
        p = self.plan({"A": {bytes([i]): 1 for i in range(12)}})
        for fault in ("after_archive", "before_commit", "after_commit"):
            with self.subTest(fault=fault):
                with self.create(p, name=fault) as c:
                    c.run_batch(3)
                    c.checkpoint()
                    c.run_batch(2)
                    before, head = copy.deepcopy(c.state), c.head
                program = ("from checkpoint_runner import CheckpointCampaign; import os,sys; c=CheckpointCampaign(sys.argv[1]);"
                           "\ntry: c.checkpoint(fault=sys.argv[2])"
                           "\nexcept RuntimeError: os._exit(86)")
                child = subprocess.run([os.sys.executable, "-B", "-c", program, str(self.root / fault), fault],
                                       cwd=HERE, capture_output=True, timeout=20)
                self.assertEqual(child.returncode, 86, child.stderr)
                with CheckpointCampaign(self.root / fault) as c:
                    self.assertEqual(c.state, before)
                    self.assertEqual(c.head, head)
                    self.assertEqual(c.state["checked_negative"], 5)
                    self.assertEqual(c.tail_count, 0 if fault == "after_commit" else 2)
                    self.assertTrue(c.audit()["verified"])
                    c.checkpoint()
                    self.assertEqual(c.run_batch(2)["job"]["start"], 5)

    def test_real_process_death_inside_uncommitted_sql_transaction(self):
        p = self.plan({"A": {bytes([i]): 1 for i in range(10)}})
        with self.create(p) as c:
            c.run_batch(2)
            c.checkpoint()
            c.run_batch(2)
            before = copy.deepcopy(c.state)
        # Abrupt exit while SQLite changes are still uncommitted, not after an
        # exception handler rolled them back. WAL recovery must restore both tables.
        code = ("import sqlite3,os,sys; d=sqlite3.connect(sys.argv[1],isolation_level=None);"
                "d.execute('BEGIN IMMEDIATE');d.execute('DELETE FROM tail');"
                "d.execute('UPDATE checkpoint SET body=?',(b\"damaged\",));os._exit(87)")
        p2 = subprocess.run([os.sys.executable, "-B", "-c", code, str(self.root / "campaign/checkpoint.sqlite")], timeout=20)
        self.assertEqual(p2.returncode, 87)
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertEqual(c.state, before)
            self.assertTrue(c.audit()["verified"])

    def test_corrupt_missing_checkpoint_archive_plan_tail_are_rejected(self):
        p = self.plan({"A": {bytes([i]): 1 for i in range(10)}})
        for kind in ("checkpoint", "archive", "missing-archive", "plan", "tail", "missing-tail", "missing-checkpoint"):
            with self.subTest(kind=kind):
                with self.create(p, name=kind) as c:
                    c.run_batch(2)
                    c.checkpoint()
                    archive = c.archive_path(c.archives[0]["sha256"])
                    plan_path = c.blob(c.state["plan"])
                    c.run_batch(1)
                db_path = self.root / kind / "checkpoint.sqlite"
                if kind in ("checkpoint", "tail", "missing-tail", "missing-checkpoint"):
                    with sqlite3.connect(db_path) as db:
                        if kind == "checkpoint":
                            db.execute("UPDATE checkpoint SET body=?", (b"{}",))
                        elif kind == "tail":
                            db.execute("UPDATE tail SET body=? WHERE seq=(SELECT max(seq) FROM tail)", (b"{}",))
                        elif kind == "missing-tail":
                            db.execute("DELETE FROM tail WHERE seq=(SELECT max(seq) FROM tail)")
                        else:
                            db.execute("DELETE FROM checkpoint")
                elif kind == "missing-archive":
                    archive.rename(archive.with_suffix(".missing"))
                else:
                    (archive if kind == "archive" else plan_path).write_bytes(b"corrupt")
                with self.assertRaises((RuntimeError, ValueError, sqlite3.DatabaseError)):
                    CheckpointCampaign(self.root / kind)

    def test_checkpoint_replay_rejects_false_in_memory_coverage(self):
        p = self.plan({"A": {bytes([i]): 1 for i in range(10)}})
        with self.create(p) as c:
            c.run_batch(2)
            c.state["checked_negative"] = 3
            c.state["cursor"] = 3
            c.state["ranges"][c.state["plan"]] = [(0, 3)]
            with self.assertRaisesRegex(RuntimeError, "replay disagrees"):
                c.checkpoint()
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertEqual(c.state["checked_negative"], 2)

    def test_full_audit_detects_rewritten_snapshot_even_with_recomputed_checksum(self):
        p = self.plan({"A": {bytes([i]): 1 for i in range(10)}})
        with self.create(p) as c:
            c.run_batch(2)
            c.checkpoint()
        path = self.root / "campaign/checkpoint.sqlite"
        with sqlite3.connect(path) as db:
            cp = json.loads(db.execute("SELECT body FROM checkpoint").fetchone()[0])
            cp["state"]["cursor"] = 3
            cp["state"]["checked_negative"] = 3
            cp["state"]["ranges"][cp["state"]["plan"]] = [[0, 3]]
            body = canonical(cp)
            db.execute("UPDATE checkpoint SET body=?,digest=?", (body, sha_bytes(body)))
        # A deliberate metadata+checksum rewrite is outside fast-resume's
        # accidental-corruption model; the separate full audit must expose it.
        with CheckpointCampaign(self.root / "campaign") as c:
            with self.assertRaisesRegex(RuntimeError, "does not match receipt replay"):
                c.audit()

    def test_automatic_compaction_across_many_epochs(self):
        p = self.plan({"A": {bytes([i]): 1 for i in range(30)}})
        with self.create(p, checkpoint_every=4) as c:
            for _ in range(12):
                c.run_batch(1)
            self.assertEqual(c.sequence, 25)
            self.assertLess(c.tail_count, 4)
            self.assertEqual(c.state["checked_negative"], 12)
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertLess(c.replayed_events, 4)
            self.assertTrue(c.audit()["verified"])

    def test_twelve_revisions_checkpoints_and_restarts_match_set_oracle(self):
        rng = random.Random(912_309)
        universe = [bytes([i]) for i in range(30)]
        done = set()
        for revision in range(12):
            d = {str(i): {v: rng.randint(1, 9) for v in rng.sample(universe, 16)} for i in range(3)}
            weights = {str(i): rng.randint(1, 8) for i in range(3)}
            plan = self.plan(d, weights)
            c = self.create(plan) if revision == 0 else CheckpointCampaign(self.root / "campaign")
            with c:
                if revision:
                    c.revise(plan)
                want = expected(d, weights, done)
                self.assertEqual(rows(c.blob(c.state["plan"]), 0, len(want)), want)
                if want:
                    receipt = c.run_batch(2)
                    added = {v for v, _ in want[:receipt["committed_negative"]]}
                    self.assertTrue(done.isdisjoint(added))
                    done.update(added)
                c.checkpoint()
                self.assertEqual(c.state["checked_negative"], len(done))
                self.assertTrue(c.audit()["verified"])

    def test_import_v1_copy_preserves_pending_and_source_bytes(self):
        p = self.plan({"A": {bytes([i]): 1 for i in range(10)}})
        with previous.RunnerTests.create(self, p, name="original") as old:
            old.run_batch(2)
            with self.assertRaises(RuntimeError):
                old.run_batch(3, fault="before_commit")
            before = copy.deepcopy(old.state)
        source = self.root / "original"
        hashes = {str(f.relative_to(source)): sha(f) for f in source.rglob("*") if f.is_file()}
        imported = import_v1_copy(source, self.root / "upgraded")
        self.assertEqual(imported["status"]["checked_negative"], 2)
        self.assertEqual({str(f.relative_to(source)): sha(f) for f in source.rglob("*") if f.is_file()}, hashes)
        with CheckpointCampaign(self.root / "upgraded") as c:
            self.assertEqual(c.state, before)
            self.assertTrue(c.audit()["verified"])
            self.assertEqual(c.run_batch(1)["job"], before["pending"])
            c.checkpoint()
        # Existing v1 evidence remains readable with its original runner.
        with OriginalCampaign(source) as old:
            self.assertEqual(old.state, before)

    def test_failed_import_does_not_publish_or_change_source(self):
        p = self.plan({"A": {b"a": 1}})
        with previous.RunnerTests.create(self, p, name="original") as old:
            old.run_batch(1)
        source = self.root / "original"
        with sqlite3.connect(source / "history.sqlite") as db:
            db.execute("UPDATE events SET body=? WHERE seq=3", (b"{}",))
        hashes = {str(f.relative_to(source)): sha(f) for f in source.rglob("*") if f.is_file()}
        with self.assertRaises(RuntimeError):
            import_v1_copy(source, self.root / "failed-copy")
        self.assertFalse((self.root / "failed-copy").exists())
        self.assertEqual({str(f.relative_to(source)): sha(f) for f in source.rglob("*") if f.is_file()}, hashes)

    def test_lock_disk_guard_and_simulation_domain(self):
        p = self.plan({"A": {b"a": 1, b"b": 1}})
        with self.create(p) as c:
            with self.assertRaisesRegex(RuntimeError, "already has an owner"):
                CheckpointCampaign(c.root)
            c.run_batch(1)
            with patch("checkpoint_runner.disk_guard", side_effect=RuntimeError("disk reserve")):
                with self.assertRaisesRegex(RuntimeError, "disk reserve"):
                    c.checkpoint()
            self.assertEqual(c.db.execute("SELECT count(*) FROM tail").fetchone()[0], 3)
        with self.create(p, name="simulation", evidence_domain="SIMULATED-history-NO-CHECKS") as c:
            with self.assertRaisesRegex(RuntimeError, "simulation history"):
                c.run_batch(1)


if __name__ == "__main__":
    unittest.main()
