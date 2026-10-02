"""Small exhaustive and fault oracles for automatic history. All targets are fake."""
import copy
import json
import os
from pathlib import Path
import random
import subprocess
import sys
from unittest.mock import patch

from checkpoint_runner import CheckpointCampaign
from history_campaign import HistoryCampaign, FIXTURE_POLICY, REHEARSAL
import coverage_snapshot as coverage
from runner import canonical, sha
import test_runner as old
from test_core import BIN, expected, rows

GEOMETRY = Path(os.environ.get("LEGACY_GEOMETRY", coverage.GEOMETRY)).resolve()


class HistoryCampaignTests(old.unittest.TestCase):
    setUp = old.RunnerTests.setUp
    plan = old.RunnerTests.plan

    def create(self, plan, *, history=None, limit=1, secret=b"fake-secret-absent", name="campaign", checkpoint_every=0):
        target = self.root/(name+".target")
        target.write_bytes(secret)
        if history is None:
            history = self.plan({"past": {b"a": 1, b"old": 1}})
        return HistoryCampaign.create(self.root/name, plan, target,
            [str(old.CHECKER), "check", str(limit), "ok"], [str(old.CHECKER), "confirm"],
            history=history, history_kind="synthetic-fixture", evidence_policy=FIXTURE_POLICY,
            core=BIN, geometry=GEOMETRY, checkpoint_every=checkpoint_every)

    def test_actual_lifecycle_matches_exhaustive_set_and_score_oracle(self):
        versions = [
            {"familiar": {b"a": 8, b"b": 7, b"c": 6, b"d": 1}},
            {"familiar": {b"a": 1, b"b": 1, b"c": 2, b"d": 8}, "new": {b"a": 4, b"c": 4, b"e": 8}},
            {"different-boundaries": {b"old": 99, b"c": 1, b"e": 9, b"f": 7}}]
        plans = [self.plan(v) for v in versions]
        done, base = set(), {b"a", b"old"}
        with self.create(plans[0], checkpoint_every=2):
            pass
        for stage, i in enumerate((0, 1, 2, 0, 2, 1)):
            with HistoryCampaign(self.root/"campaign", checkpoint_every=2) as c:
                if stage:
                    c.revise(plans[i])
                want = expected(versions[i], dict.fromkeys(versions[i], 1), base | done)
                self.assertEqual(rows(c.blob(c.state["plan"]), 0, len(want)), want)
                if want:
                    ack = c.run_batch(min(3, len(want)))
                    self.assertEqual(ack["committed_negative"], 1)
                    self.assertNotIn(want[0][0], done | base)
                    done.add(want[0][0])
                self.assertEqual(c.state["checked_negative"], len(done))
                self.assertEqual(c.status()["accepted_history"]["count"], 2)
                self.assertEqual(c.status()["evidence_domain"], REHEARSAL)
                c.checkpoint()
                self.assertTrue(c.audit()["verified"])

    def test_twelve_random_revisions_ignore_old_coordinates(self):
        rng = random.Random(83821)
        done, excluded = set(), {b"a", b"old"}
        for i in range(12):
            dist = {bytes([x]): rng.randrange(1, 20) for x in rng.sample(range(97, 123), 12)}
            plan = self.plan({"new": dist})
            c = self.create(plan, limit=2) if not i else HistoryCampaign(self.root/"campaign", checkpoint_every=0)
            with c:
                if i:
                    c.revise(plan)
                want = expected({"new": dist}, {"new": 1}, done | excluded)
                self.assertEqual(rows(c.blob(c.state["plan"]), 0, len(want)), want)
                if want:
                    k = c.run_batch(min(4, len(want)))["committed_negative"]
                    done.update(x for x, _ in want[:k])
                c.checkpoint()
        with HistoryCampaign(self.root/"campaign") as c:
            self.assertEqual(c.state["checked_negative"], len(done))
            self.assertTrue(c.audit()["verified"])

    def test_interrupted_ack_is_retried_durable_ack_is_not(self):
        plan = self.plan({"a": {bytes([x]): 200-x for x in range(97, 110)}})
        with self.create(plan, limit=2) as c:
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                c.run_batch(4, fault="before_commit")
            pending = copy.deepcopy(c.state["pending"])
            self.assertEqual(c.state["checked_negative"], 0)
            c.checkpoint()
        with HistoryCampaign(self.root/"campaign") as c:
            ack = c.run_batch(1)
            self.assertEqual(ack["job"], pending)
            self.assertEqual(ack["committed_negative"], 2)
            with self.assertRaisesRegex(RuntimeError, "durable commit"):
                c.run_batch(4, fault="after_commit")
        with HistoryCampaign(self.root/"campaign") as c:
            self.assertEqual(c.state["cursor"], 4)
            self.assertEqual(c.state["checked_negative"], 4)
            c.revise(plan)
            self.assertEqual(c.preview(count=1)["candidates"][0]["hex"], b"f".hex())

    def test_process_death_checkpoint_and_restart(self):
        p = self.plan({"x": {b"a": 8, b"b": 4, b"c": 2, b"d": 1}})
        with self.create(p) as c:
            c.run_batch(2)
            state = copy.deepcopy(c.state)
        code = ("import os,sys; from history_campaign import HistoryCampaign; c=HistoryCampaign(sys.argv[1]);"
                "\ntry: c.checkpoint(fault='after_commit')"
                "\nexcept RuntimeError: os._exit(86)")
        child = subprocess.run([sys.executable, "-B", "-c", code, str(self.root/"campaign")], cwd=old.HERE, timeout=30)
        self.assertEqual(child.returncode, 86)
        with HistoryCampaign(self.root/"campaign") as c:
            self.assertEqual(c.state, state)
            self.assertEqual(c.run_batch(2)["job"]["start"], 1)
            self.assertTrue(c.audit()["verified"])

    def test_missing_or_changed_history_blocks_resume_and_live_revision(self):
        p = self.plan({"x": {b"a": 3, b"b": 2, b"c": 1}})
        for damage in ("language", "metadata"):
            with self.create(p, name=damage) as c:
                c.run_batch(2)
                c.checkpoint()
                state, head = copy.deepcopy(c.state), c.head
                h = c.accepted_history()
                path = c.blob(h["plan"]) if damage == "language" else c.blob(c.context["accepted_history"], "evidence")
                original = path.read_bytes()
                path.write_bytes(b"damaged")
                with self.assertRaises(RuntimeError):
                    c.revise(p)
                self.assertEqual((c.state, c.head), (state, head))
                path.write_bytes(original)
            path.rename(path.with_suffix(".unavailable"))
            with self.assertRaises((RuntimeError, FileNotFoundError)):
                HistoryCampaign(self.root/damage)

    def test_read_only_preview_equals_adopted_ranking(self):
        p = self.plan({"x": {b"a": 9, b"b": 8, b"c": 7}})
        q = self.plan({"y": {b"a": 99, b"b": 90, b"d": 80, b"c": 1}})
        with self.create(p) as c:
            c.run_batch(2)
            state, head, seq = copy.deepcopy(c.state), c.head, c.sequence
            files = set(c.root.rglob("*"))
            preview = c.preview(q)
            self.assertEqual([x["text"] for x in preview["candidates"]], ["d", "c"])
            self.assertEqual((c.state, c.head, c.sequence), (state, head, seq))
            self.assertEqual(set(c.root.rglob("*")), files)
            c.revise(q)
            self.assertEqual(c.preview()["candidates"], preview["candidates"])

    def test_false_revision_cannot_bypass_base_or_local_history(self):
        p = self.plan({"x": {b"a": 8, b"b": 4, b"c": 2}})
        with self.create(p) as c:
            c.run_batch(2)
            full = c.archive(p, "plans")
            before = copy.deepcopy(c.state), c.head
            with self.assertRaisesRegex(RuntimeError, "minus accepted/completed"):
                c.append({"kind": "revise", "full": full, "remaining": full, "history_head": c.head})
            self.assertEqual((c.state, c.head), before)

    def test_preparation_storage_failure_does_not_change_campaign(self):
        p = self.plan({"x": {b"a": 8, b"b": 4, b"c": 2}})
        with self.create(p) as c:
            before = copy.deepcopy(c.state), c.head
            with patch("history_campaign.disk_guard", side_effect=RuntimeError("test free disk reserve")):
                with self.assertRaisesRegex(RuntimeError, "disk reserve"):
                    c.preview(p)
                with self.assertRaisesRegex(RuntimeError, "disk reserve"):
                    c.revise(p)
            self.assertEqual((c.state, c.head), before)

    def test_wrong_policy_old_controller_and_true_secret_in_history_rejected(self):
        p = self.plan({"x": {b"a": 8, b"b": 4, b"c": 2}})
        with self.create(p) as c:
            c.checkpoint()
        with self.assertRaisesRegex(RuntimeError, "checkpoint engine changed"):
            CheckpointCampaign(self.root/"campaign")
        with self.assertRaisesRegex(RuntimeError, "true password"):
            self.create(p, secret=b"a", name="bad-secret")
        with self.assertRaisesRegex(RuntimeError, "policy"):
            HistoryCampaign.create(self.root/"bad-policy", p, self.root/"campaign.target",
                [str(old.CHECKER), "check", "all", "ok"], [str(old.CHECKER), "confirm"],
                history=p, history_kind="synthetic-fixture", evidence_policy=coverage.LEGACY_POLICY, core=BIN)
        self.assertFalse((self.root/"bad-policy").exists())

    def test_hit_from_later_idea_is_confirmed_and_unfinished_tail_not_credited(self):
        p = self.plan({"old": {b"a": 8, b"b": 4, b"c": 2}})
        q = self.plan({"new": {b"a": 8, b"b": 7, b"secret": 6, b"d": 1}})
        with self.create(p, limit="all", secret=b"secret") as c:
            c.run_batch(2)
            c.revise(q)
            ack = c.run_batch(2)
            self.assertEqual((ack["committed_negative"], ack["confirmed_hits"], ack["uncredited_tail"]), (0, 1, 1))
            c.checkpoint()
        with HistoryCampaign(self.root/"campaign") as c:
            self.assertEqual(c.state["hit"]["hex"], b"secret".hex())
            self.assertEqual(c.state["checked_negative"], 2)
            self.assertTrue(c.audit()["verified"])



if __name__ == "__main__":
    old.unittest.main()
