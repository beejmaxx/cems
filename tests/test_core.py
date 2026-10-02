"""Small exact byte-set oracles are TEST ONLY. All execution/preparation is C++."""
from layout import BUILD
from fractions import Fraction as Q
import json
import os
from pathlib import Path
import random
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from fixtures import explicit_graph
from graph_fixtures import decimal_graph
from swg import write_source

from layout import LAB as HERE
from native_probe import BIN, run, rows

def expected(distributions, weights, done=()):
    result = {}
    for name, masses in distributions.items():
        for value, mass in masses.items():
            result[value] = result.get(value, Q(0)) + Q(weights[name], sum(weights.values())) * Q(mass, sum(masses.values()))
    return sorted(((v, p) for v, p in result.items() if v not in done), key=lambda r: (-r[1], r[0]))


class CoreTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="prep-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.serial = 0

    def compile(self, distributions, weights=None):
        self.serial += 1
        source, plan = self.root / f"{self.serial}.source", self.root / f"{self.serial}.plan"
        write_source(source, {n: explicit_graph(d) for n, d in distributions.items()}, weights or dict.fromkeys(distributions, 1))
        result = json.loads(run("compile", source, plan).stdout)
        return plan, result

    def test_sum_not_best_derivation_and_variable_boundaries(self):
        d = {"a": {b"abc!": 2, b"a": 3}, "b": {b"abc!": 2, b"b": 3}}
        plan, info = self.compile(d)
        self.assertEqual(info["candidates"], "3")
        self.assertEqual(rows(plan, 0, 3), expected(d, {"a": 1, "b": 1}))
        self.assertEqual(rows(plan, 0, 1)[0][0], b"abc!")

    def test_byte_delivery_zero_newline_and_nonascii(self):
        d = {"a": {b"": 1, b"\x00\n\xff": 1, b"$HEX[41]": 2, b"a\nb": 1}}
        plan, _ = self.compile(d)
        frames = run("emit", plan, 0, 4).stdout
        actual = []
        i = 0
        while i < len(frames):
            n = frames[i]; i += 1
            actual.append(frames[i:i+n]); i += n
        self.assertEqual(actual, [v for v, _ in expected(d, {"a": 1})])
        answer = json.loads(run("consume", 4, input=frames).stdout)
        self.assertEqual(answer["consumed_frames"], 4)
        for corrupt in (frames[:-1], frames + b"\x00", b"\xff"):
            with self.assertRaises(subprocess.CalledProcessError):
                run("consume", 4, input=corrupt)

    def test_receiver_chunk_boundaries_and_independent_checksum(self):
        values = [b"", bytes(range(128)), b"x", b"\x00\n\xff"] * 1025
        framed = b"".join(bytes([len(v)]) + v for v in values)
        checksum = 14695981039346656037
        for byte in framed:
            checksum = ((checksum ^ byte) * 1099511628211) & (2**64-1)
        answer = json.loads(run("consume", len(values), input=framed).stdout)
        self.assertEqual(int(answer["fnv64"]), checksum)
        self.assertEqual(answer["candidate_bytes"], sum(map(len, values)))
        self.assertEqual(answer["consumed_frames"], len(values))
        for cut in (65535, 65536, 65537, len(framed)-1):
            with self.assertRaises(subprocess.CalledProcessError):
                run("consume", len(values), input=framed[:cut])

    def test_random_mixtures_partial_history_and_reweight(self):
        rng = random.Random(20260929)
        universe = [b"", b"a", b"aa", b"ab", b"b", b"ba", b"x", b"xy", b"xyz"]
        for _ in range(60):
            d = {str(i): {v: rng.randint(1, 9) for v in rng.sample(universe, rng.randint(1, len(universe)))}
                 for i in range(rng.randint(1, 4))}
            weights = {n: rng.randint(1, 5) for n in d}
            old, info = self.compile(d, weights)
            all_rows = expected(d, weights)
            self.assertEqual(rows(old, 0, len(all_rows)), all_rows)
            self.assertEqual(int(info["candidates"]), len(all_rows))
            first = len(all_rows)//3
            ranges = [(0, first), (len(all_rows)-1, 1)]
            done = {v for start, count in ranges for v, _ in all_rows[start:start+count]}
            d["new"] = {b"abc": 8, b"xy": 5, b"a": 3}
            weights = {n: rng.randint(1, 5) for n in d}
            new, _ = self.compile(d, weights)
            eligible = self.root / f"eligible-{self.serial}.plan"
            result = json.loads(run("subtract", new, eligible, *[x for start, count in ranges for x in (old, start, count)]).stdout)
            wanted = expected(d, weights, done)
            self.assertEqual(int(result["candidates"]), len(wanted))
            self.assertEqual(rows(eligible, 0, len(wanted)), wanted)
            # Prepared but unfinished positions did not enter history. A fresh
            # process reloads the unchanged plan; weights never redefine old ranks.
            middle = len(wanted)//2
            self.assertEqual(rows(eligible, middle, len(wanted)-middle), wanted[middle:])
            # Reimporting the same completion is set-idempotent.
            again = self.root / f"again-{self.serial}.plan"
            run("subtract", eligible, again, old, 0, first)
            self.assertEqual(rows(again, 0, len(wanted)), wanted)

    def test_large_compact_ranges_and_eight_disjoint_assignments(self):
        source, old = self.root / "decimal.source", self.root / "decimal.plan"
        write_source(source, {"d": decimal_graph()}, {"d": 1})
        result = json.loads(run("compile", source, old).stdout)
        self.assertEqual(int(result["candidates"]), 10**14)
        self.assertLess(result["file_bytes"], 2048)
        self.assertEqual(rows(old, 10**14-1, 1), [(b"99999999999999", Q(1, 10**14))])
        eligible = self.root / "remaining.plan"
        result = json.loads(run("subtract", old, eligible, old, 0, 9*10**13, old, 9*10**13+1000, 500).stdout)
        self.assertEqual(int(result["candidates"]), 10**13-500)
        self.assertEqual(rows(eligible, 999, 2)[0][0], b"90000000000999")
        self.assertEqual(rows(eligible, 999, 2)[1][0], b"90000000001500")
        parts = [rows(eligible, worker*50, 50) for worker in range(8)]
        self.assertEqual([r for part in parts for r in part], rows(eligible, 0, 400))
        self.assertEqual(len({v for part in parts for v, _ in part}), 400)
        self.assertLess(eligible.stat().st_size, 4096)

    def test_partitioning_an_explanation_preserves_mass(self):
        a, _ = self.compile({"A": {b"x": 2, b"y": 1}}, {"A": 10})
        b, _ = self.compile({"A1": {b"x": 2, b"y": 1}, "A2": {b"x": 2, b"y": 1}}, {"A1": 3, "A2": 7})
        self.assertEqual(rows(a, 0, 2), rows(b, 0, 2))

    def test_bounded_exact_score_levels_and_next_preparation(self):
        rng = random.Random(923)
        universe = [bytes([97+a, 97+b]) for a in range(4) for b in range(4)]
        for trial in range(25):
            distributions = {str(i): {v: rng.randint(1, 20) for v in rng.sample(universe, 12)} for i in range(3)}
            weights = {"0": 2, "1": 3, "2": 1}
            self.serial += 1
            source = self.root / f"bounded-{trial}.source"
            write_source(source, {n: explicit_graph(d) for n, d in distributions.items()}, weights)
            done, history = set(), []
            for revision in range(3):
                plan = self.root / f"bounded-{trial}-{revision}.plan"
                result = json.loads(run("prepare", source, plan, 2, *history).stdout)
                ordered = expected(distributions, weights, done)
                scores = sorted({p for _, p in ordered}, reverse=True)[:2]
                wanted = [(v, p) for v, p in ordered if p in scores]
                self.assertEqual(rows(plan, 0, len(wanted)), wanted)
                self.assertEqual(int(result["candidates"]), len(wanted))
                self.assertEqual(result["complete_remaining_support"], len(wanted) == len(ordered))
                # A partial prefix, not the whole prepared plan, is the next history.
                count = max(1, len(wanted)//2)
                history += [plan, 0, count]
                done.update(v for v, _ in wanted[:count])

    def test_preparing_past_a_huge_equal_score_band(self):
        source = self.root / "huge.source"
        write_source(source, {"decimal": decimal_graph(), "extra": explicit_graph({b"highest": 1})},
                     {"decimal": 1, "extra": 1})
        first = self.root / "huge-first.plan"
        r = json.loads(run("prepare", source, first, 1).stdout)
        self.assertEqual(int(r["candidates"]), 1)
        self.assertFalse(r["complete_remaining_support"])
        second = self.root / "huge-second.plan"
        r = json.loads(run("prepare", source, second, 1, first, 0, 1).stdout)
        self.assertEqual(int(r["candidates"]), 10**14)
        self.assertTrue(r["complete_remaining_support"])
        self.assertLess(second.stat().st_size, 2048)
        self.assertEqual(rows(second, 10**14-1, 1), [(b"99999999999999", Q(1, 2*10**14))])

    def test_reintroduction_revocation_and_out_of_order_completion(self):
        distributions = {"a": {b"a": 6, b"b": 5, b"c": 4, b"d": 3, b"e": 2, b"f": 1}}
        original, _ = self.compile(distributions)
        # Prepared all six; only a,b and e got simulated acknowledgements.
        old_done = {b"a", b"b", b"e"}
        second_d = {"b": {b"a": 7, b"c": 8, b"new": 9}}
        second, _ = self.compile(second_d)
        eligible = self.root / "second-eligible.plan"
        run("subtract", second, eligible, original, 4, 1, original, 0, 2)
        self.assertEqual(rows(eligible, 0, 2), expected(second_d, {"b": 1}, old_done))
        # Only c (the later assignment) completed, not the prepared leading new.
        self.assertEqual(rows(eligible, 1, 1)[0][0], b"c")
        third_d = {"c": {b"a": 3, b"c": 8, b"new": 11, b"d": 4, b"e": 7, b"f": 6}}
        third, _ = self.compile(third_d)
        remaining = self.root / "third-eligible.plan"
        run("subtract", third, remaining, eligible, 1, 1, original, 4, 1, original, 0, 2)
        wanted = expected(third_d, {"c": 1}, old_done | {b"c"})
        self.assertEqual(rows(remaining, 0, len(wanted)), wanted)
        self.assertEqual(wanted[0][0], b"new")
        # Withdrawal is modeled by rebuilding from accepted evidence, not by
        # trusting a previously subtracted plan to restore removed candidates.
        restored = self.root / "third-withdrawn.plan"
        run("subtract", third, restored, eligible, 1, 1)
        wanted = expected(third_d, {"c": 1}, {b"c"})
        self.assertEqual(rows(restored, 0, len(wanted)), wanted)
        self.assertIn(b"a", {v for v, _ in wanted})

    def test_shared_choice_not_independent_occurrences(self):
        # Same separator selected once vs independent separators. Explicit byte
        # distributions independently define what the C++ graphs must preserve.
        shared = {b"a-a-": 1, b"a_a_": 1}
        independent = {a+b for a in (b"a-", b"a_") for b in (b"a-", b"a_")}
        d = {"shared": shared, "independent": dict.fromkeys(independent, 1)}
        plan, _ = self.compile(d, {"shared": 3, "independent": 1})
        self.assertEqual(rows(plan, 0, 4), expected(d, {"shared": 3, "independent": 1}))

    def test_arithmetic_overflow_rejects_without_a_plan(self):
        source = self.root / "overflow.source"
        source.write_text(f"SWG1 1 2 1\n0 1\n0 1 97 1 {2**128-1}\n2 0\n")
        with self.assertRaises(subprocess.CalledProcessError):
            run("compile", source, self.root / "overflow.plan")
        self.assertFalse((self.root / "overflow.plan").exists())

    def test_disk_reservation_precedes_native_publication(self):
        import bench
        with patch.object(bench, "CAP", 1024):
            self.assertEqual(bench.disk_guard(self.root, 1024), 0)
            with self.assertRaises(RuntimeError):
                bench.disk_guard(self.root, 1025)
            with patch.object(bench.subprocess, "run") as launch:
                with self.assertRaises(RuntimeError):
                    bench.invoke("compile", self.root / "unused.source", self.root / "never.plan")
                launch.assert_not_called()

    def test_refuse_overwrite_bad_ranges_and_corrupt_input(self):
        old, _ = self.compile({"a": {b"a": 1, b"b": 1}})
        before = old.read_bytes()
        with self.assertRaises(subprocess.CalledProcessError):
            run("subtract", old, old)
        self.assertEqual(old.read_bytes(), before)
        for start, count in ((3, 0), (1, 2), (0, 2**64), (-1, 1)):
            with self.assertRaises(subprocess.CalledProcessError):
                run("probe", old, start, count)
        bad = self.root / "corrupt.plan"
        bad.write_bytes(before[:-1])
        with self.assertRaises(subprocess.CalledProcessError):
            run("describe", bad)
        source = self.root / "cyclic.source"
        source.write_text("SWG1 1 1 1\n0 1\n0 1 97 0 1\n")
        with self.assertRaises(subprocess.CalledProcessError):
            run("compile", source, self.root / "no.plan")
        self.assertFalse((self.root / "no.plan").exists())


if __name__ == "__main__":
    os.umask(0o077)
    unittest.main()
