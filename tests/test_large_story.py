"""Exhaustive reduced-domain checks for the large recipe experiment/oracle."""
from layout import BUILD
from fractions import Fraction as Q
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import unittest

from large_story import audit_stream
from large_story_model import SMALL, RecipeModel, RecipeHistory, RecipeIndex, inputs, secret
from model_workflow import build_bundle
from runner import HERE
from test_core import BIN

AUDITOR = Path(os.environ.get("RECIPE_AUDITOR", BUILD / "recipe-audit")).resolve()


def explicit(model):
    result = {}
    for (before, after), pieces in model.pieces.items():
        for lo, hi, score in pieces:
            for n in range(lo, hi):
                value = before + f"{n:0{model.cfg.digits}d}".encode() + after
                if value in result:
                    raise AssertionError("probability-piece overlap")
                result[value] = score
    return result


class LargeStoryTests(unittest.TestCase):
    def test_five_small_models_all_bytes_scores_and_revised_history(self):
        rng, history, done = random.Random(1001), RecipeHistory(), set()
        with tempfile.TemporaryDirectory(prefix="large-recipe-small-") as temporary:
            root = Path(temporary)
            for stage in range(5):
                with self.subTest(stage=stage):
                    model = RecipeModel(stage, SMALL)
                    all_values = explicit(model)
                    expected = sorted(((v, p) for v, p in all_values.items() if v not in done), key=lambda row: (-row[1], row[0]))
                    index = RecipeIndex(model, history)
                    self.assertEqual(index.count, len(expected))
                    self.assertEqual([index.at(i) for i in range(index.count)], expected)
                    self.assertTrue(all(index.rank(v) == i for i, (v, _) in enumerate(expected)))
                    self.assertTrue(all(index.rank(v) is None for v in done))
                    bundle = root / f"model-{stage}"
                    build_bundle(inputs(stage, SMALL), bundle, core=BIN)
                    # Native subtraction uses prior immutable plans/ranges, not
                    # the independent oracle's explicit small test set.
                    active = root / f"remaining-{stage}.plan"
                    command = [str(BIN), "subtract", str(bundle / "model.plan"), str(active)]
                    for p, a, n in native_history if stage else []:
                        command += [str(p), str(a), str(n)]
                    subprocess.run(command, check=True, capture_output=True, timeout=100)
                    for start in range(0, len(expected), 10000):
                        n = min(10000, len(expected)-start)
                        result = subprocess.run([str(BIN), "inspect", str(active), str(start), str(n)],
                                                capture_output=True, check=True, timeout=100)
                        self.assertLess(len(result.stdout), 4*1024**2)
                        actual = [(bytes.fromhex(v), Q(int(p), int(d))) for v, p, d in
                                  (line.split(" ") for line in result.stdout.decode().splitlines())]
                        self.assertEqual(actual, expected[start:start+n])
                    oracle = root / f"oracle-{stage}.recipes"
                    index.export(oracle)
                    self.assertEqual(audit_stream(BIN, AUDITOR, active, oracle, 0, index.count)["frames_compared_exactly"], index.count)
                    if stage == 0:
                        native_history = []
                    # Deliberate gaps, non-prefix completions, and partial numeric
                    # cycles exercise the independent interval algebra.
                    slices = [(0, 19), (index.count//3, 31), (index.count-13, 13)]
                    for a, n in slices:
                        history.add(index, a, n)
                        selected = {v for v, _ in expected[a:a+n]}
                        self.assertFalse(done & selected)
                        done.update(selected)
                        native_history.append((active, a, n))
                    self.assertEqual(history.count, len(done))
                    # Random queries also probe exclusions absent from this model.
                    for value in rng.sample(list(all_values), 20):
                        self.assertEqual(index.rank(value) is None, value in (done-set(v for a,n in slices for v,_ in expected[a:a+n])))

    def test_auditor_detects_every_tested_stream_fault_and_handles_deep_seek(self):
        index = RecipeIndex(RecipeModel(4))
        with tempfile.TemporaryDirectory(prefix="recipe-audit-") as temporary:
            path = Path(temporary) / "oracle.recipes"
            index.export(path)
            start = min(10**12, index.count-3)
            values = [index.at(start+i)[0] for i in range(3)]

            def frames(items):
                return b"".join(bytes([len(v)])+v for v in items)

            command = [str(AUDITOR), str(path), str(start), "3"]
            good = frames(values)
            answer = subprocess.run(command, input=good, capture_output=True, check=True, timeout=30)
            self.assertEqual(json.loads(answer.stdout)["frames_compared_exactly"], 3)
            corrupt = [frames([values[1], values[0], values[2]]), frames([values[0], values[0], values[2]]),
                       good[:-1], good+b"\x00", good[1:], frames([values[0]+b"x", *values[1:]])]
            for raw in corrupt:
                result = subprocess.run(command, input=raw, capture_output=True, timeout=30)
                self.assertNotEqual(result.returncode, 0)
                self.assertEqual(result.stdout, b"")
            with path.open("ab") as f:
                f.write(b"unexpected\n")
            self.assertNotEqual(subprocess.run(command, input=good, capture_output=True, timeout=30).returncode, 0)

    def test_history_prefix_arithmetic_against_random_explicit_sets(self):
        rng = random.Random(888)
        history, done = RecipeHistory(), set()
        for turn in range(20):
            model = RecipeModel(turn % 5, SMALL)
            all_values = explicit(model)
            index = RecipeIndex(model, history)
            expected = sorted(((v, p) for v, p in all_values.items() if v not in done), key=lambda row: (-row[1], row[0]))
            self.assertEqual(index.count, len(expected))
            for _ in range(10):
                rank = rng.randrange(index.count)
                self.assertEqual(index.at(rank), expected[rank])
            start, count = rng.randrange(index.count-100), rng.randrange(1, 100)
            history.add(index, start, count)
            done.update(v for v, _ in expected[start:start+count])
            self.assertEqual(history.count, len(done))
        with self.assertRaisesRegex(RuntimeError, "checked twice"):
            history.add(index, start, count)

    def test_same_clues_admit_then_withdraw_then_restore_generated_targets(self):
        models = [RecipeModel(stage, SMALL) for stage in range(5)]
        for seed in range(20):
            value, _ = secret(seed, SMALL)
            self.assertEqual([bool(m.score(value)) for m in models], [False, True, False, True, True])


if __name__ == "__main__":
    unittest.main()
