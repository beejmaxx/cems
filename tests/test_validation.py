"""Independent malformed-plan construction; no reliance on compiler invariants."""
from layout import BUILD, source as source_path
import copy
import json
import os
from pathlib import Path
import random
import struct
import subprocess
import tempfile
import unittest

from checkpoint_runner import CheckpointCampaign
from fixtures import explicit_graph, write_source
from test_runner import CHECKER

from layout import LAB as HERE
CHECKED = Path(os.environ.get("PREP_BINARY", BUILD / "prep-workspace")).resolve()


def plan_bytes(bands):
    """Direct PLAB0002 writer, intentionally permits intersections between bands.

    Separate tries and deliberately unshared node IDs also test semantic, not
    merely root-ID, equality. Only a small exhaustive oracle uses these sets.
    """
    nodes = [(False, []), (True, [])]
    roots = []
    for score, values in bands:
        def build(strings):
            children = {}
            terminal = b"" in strings
            for value in strings:
                if value:
                    children.setdefault(value[0], set()).add(value[1:])
            edges = [(byte, build(tails)) for byte, tails in sorted(children.items())]
            nodes.append((terminal, edges))
            return len(nodes) - 1
        roots.append((score, build(set(values))))
    denominator = sum(score * len(set(values)) for score, values in bands)
    raw = bytearray(b"PLAB0002" + denominator.to_bytes(16, "little"))
    raw += struct.pack("<IIB", len(nodes), len(roots), 1) + bytes(16)
    for terminal, edges in nodes:
        raw += struct.pack("<BH", terminal, len(edges))
        for byte, child in edges:
            raw += struct.pack("<BI", byte, child)
    for score, root in roots:
        raw += score.to_bytes(16, "little") + struct.pack("<I", root)
    return bytes(raw)


def invoke(*args):
    return subprocess.run([str(CHECKED), *map(str, args)], capture_output=True, timeout=100)


class ValidationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="plan-validation-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)

    def test_intersection_rejected_before_any_output_or_publication(self):
        bad, good = self.root / "bad.plan", self.root / "good.plan"
        bad.write_bytes(plan_bytes([(2, {b"x"}), (1, {b"x"})]))
        good.write_bytes(plan_bytes([(1, {b"a", b"b"})]))
        source = self.root / "source.swg"
        write_source(source, {"a": explicit_graph({b"a": 1, b"b": 1})}, {"a": 1})
        output = self.root / "must-not-exist.plan"
        for args in (("describe", bad), ("inspect", bad, 0, 2), ("probe", bad, 0, 2),
                     ("emit", bad, 0, 2), ("subtract", bad, output),
                     ("subtract", good, output, bad, 0, 1),
                     ("prepare", source, output, 1, bad, 0, 1)):
            with self.subTest(args=args):
                result = invoke(*args)
                self.assertEqual(result.returncode, 2)
                self.assertEqual(result.stdout, b"")
                self.assertIn(b"overlapping score-band languages", result.stderr)
                self.assertFalse(output.exists())

    def test_overlap_with_unequal_roots_terminal_prefixes_and_deep_witness(self):
        cases = [({b"abc", b"left"}, {b"abc", b"right"}),
                 ({b"", b"x"}, {b"", b"y"}),
                 ({b"a", b"aa"}, {b"a", b"ab"}),
                 ({b"\0\n\xff", b"first"}, {b"\0\n\xff", b"second"}),
                 ({b"a" * 128, b"left"}, {b"a" * 128, b"right"})]
        for i, (left, right) in enumerate(cases):
            p = self.root / f"bad-{i}.plan"
            p.write_bytes(plan_bytes([(2, left), (1, right)]))
            result = invoke("describe", p)
            self.assertEqual(result.returncode, 2, result)
            self.assertIn(b"overlapping score-band languages", result.stderr)

    def test_shared_suffixes_and_prefixes_are_not_false_positives(self):
        bands = [(3, {b"", b"ac", b"aXYZ"}), (2, {b"a", b"bc", b"bXYZ"}),
                 (1, {b"ab", b"abc", b"cXYZ"})]
        p = self.root / "good.plan"
        p.write_bytes(plan_bytes(bands))
        result = invoke("inspect", p, 0, 9)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual([bytes.fromhex(line.split(b" ")[0].decode()) for line in result.stdout.splitlines()],
                         [v for _, values in bands for v in sorted(values)])
        info = json.loads(invoke("describe", p).stdout)
        self.assertEqual(info["validation_calls"], 1)
        self.assertGreater(info["validation_states"], 0)

    def test_random_band_languages_against_exhaustive_intersections(self):
        rng = random.Random(20260930)
        universe = [b"", b"a", b"b", b"aa", b"ab", b"ba", b"bb", b"abc", b"aXYZ", b"bXYZ", b"\0\n\xff"]
        for i in range(160):
            count = rng.randrange(1, 6)
            # Half use an exact disjoint partition; half allow arbitrary overlap.
            groups = [set(rng.sample(universe, rng.randrange(1, 5))) for _ in range(count)]
            if i % 2:
                chosen = rng.sample(universe, len(universe))
                groups = [set(chosen[j::count]) for j in range(count)]
            bands = [(count-j, values) for j, values in enumerate(groups)]
            p = self.root / f"random-{i}.plan"
            p.write_bytes(plan_bytes(bands))
            overlap = sum(map(len, groups)) != len(set.union(*groups))
            result = invoke("describe", p)
            self.assertEqual(result.returncode, 2 if overlap else 0, (i, result.stderr))
            if overlap:
                self.assertIn(b"overlapping score-band languages", result.stderr)
            else:
                self.assertEqual(int(json.loads(result.stdout)["candidates"]), sum(map(len, groups)))

    def test_bad_revision_preserves_durable_active_plan_and_pending_work(self):
        good, bad, target = [self.root / n for n in ("good.plan", "bad.plan", "target.bin")]
        good.write_bytes(plan_bytes([(1, {b"a", b"b", b"c", b"d"})]))
        bad.write_bytes(plan_bytes([(2, {b"x"}), (1, {b"x"})]))
        target.write_bytes(b"not-present")
        with CheckpointCampaign.create(self.root / "campaign", good, target,
                [str(CHECKER), "check", "all", "ok"], [str(CHECKER), "confirm"], core=CHECKED) as c:
            c.run_batch(1)
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                c.run_batch(2, fault="before_commit")
            before, head, sequence = copy.deepcopy(c.state), c.head, c.sequence
            with self.assertRaisesRegex(RuntimeError, "overlapping score-band languages"):
                c.revise(bad)
            self.assertEqual((c.state, c.head, c.sequence), (before, head, sequence))
            c.checkpoint()
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertEqual(c.state, before)
            self.assertEqual(c.run_batch(1)["job"], before["pending"])
            self.assertEqual(c.state["checked_negative"], 3)
            self.assertTrue(c.audit()["verified"])

    def test_bad_initial_plan_cannot_start_a_campaign(self):
        bad, target = self.root / "bad.plan", self.root / "target.bin"
        bad.write_bytes(plan_bytes([(2, {b"x"}), (1, {b"x"})]))
        target.write_bytes(b"absent")
        with self.assertRaisesRegex(RuntimeError, "overlapping score-band languages"):
            CheckpointCampaign.create(self.root / "campaign", bad, target,
                [str(CHECKER), "check", "all", "ok"], [str(CHECKER), "confirm"], core=CHECKED)
        with self.assertRaisesRegex(RuntimeError, "empty history"):
            CheckpointCampaign(self.root / "campaign")

    def test_launcher_uses_checked_reader_and_confirms_synthetic_hit(self):
        plan, target = self.root / "plan", self.root / "target"
        plan.write_bytes(plan_bytes([(1, {b"a", b"b", b"c"})]))
        target.write_bytes(b"b")
        campaign = self.root / "launched"

        def launch(*args):
            return json.loads(subprocess.run([os.sys.executable, "-B", str(source_path("worker.py")), *map(str, args)],
                capture_output=True, check=True, timeout=20).stdout)

        launch("create-synthetic", campaign, plan, target)
        with CheckpointCampaign(campaign) as c:
            self.assertEqual(c.context["core"]["argv"], [str(BUILD / "prep-workspace")])
        answer = launch("run", campaign, "--count", 3)
        self.assertEqual(answer["committed_negative"], 1)
        self.assertEqual(answer["confirmed_hits"], 1)
        self.assertEqual(launch("status", campaign)["hit"]["hex"], b"b".hex())
        self.assertTrue(launch("audit", campaign)["verified"])


if __name__ == "__main__":
    unittest.main()
