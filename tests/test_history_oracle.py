from fractions import Fraction as Q
from pathlib import Path
import random
import tempfile
import unittest

from fixtures import explicit_graph
from history_stress import InverseOracle
from test_validation import plan_bytes


class InverseTests(unittest.TestCase):
    def test_inverse_rank_vs_exhaustive_order_and_holes(self):
        rng = random.Random(1821)
        universe = [b"", b"a", b"aa", b"ab", b"b", b"bc", b"X", b"xyz", b"\0\n\xff"]
        with tempfile.TemporaryDirectory(prefix="inverse-oracle-") as td:
            path = Path(td) / "plan"
            for _ in range(80):
                masses = {v: rng.randint(1, 5) for v in rng.sample(universe, rng.randint(1, len(universe)))}
                # The tiny plan writer chooses denominator=sum(plan masses).
                # Use full distribution first, then represent holes by giving
                # a higher total model mass equal to the serialized denominator.
                bands = [(w, {v for v, weight in masses.items() if weight == w}) for w in sorted(set(masses.values()), reverse=True)]
                path.write_bytes(plan_bytes(bands))
                original = {"g": explicit_graph(masses)}
                index = InverseOracle(path, original, {"g": 1})
                wanted = sorted(masses, key=lambda v: (-masses[v], v))
                self.assertEqual(index.count, len(wanted))
                for value in universe:
                    self.assertEqual(index.rank(value), wanted.index(value) if value in masses else None)
                keep = wanted[::2]
                bands = [(w, {v for v in keep if masses[v] == w}) for w in sorted(set(masses[v] for v in keep), reverse=True)]
                raw = bytearray(plan_bytes(bands))
                raw[8:24] = sum(masses.values()).to_bytes(16, "little")
                path.write_bytes(raw)
                index = InverseOracle(path, original, {"g": 1})
                for value in universe:
                    self.assertEqual(index.rank(value), keep.index(value) if value in keep else None)
                for value in keep:
                    self.assertEqual(index.score(value), Q(masses[value], sum(masses.values())))


if __name__ == "__main__":
    unittest.main()
