from fractions import Fraction as Q
import random
import unittest

from fixtures import explicit_graph, probability
from stress_oracle import HeapIndex, prefixed, swapped_case, wrapped_prefix_free


class OracleTests(unittest.TestCase):
    def test_heap_order_and_counts_against_exhaustive_strings(self):
        rng = random.Random(773)
        values = [b"", b"a", b"A", b"ab", b"abc", b"ba", b"B", b"\0\xff"]
        for _ in range(80):
            masses = {s: rng.randint(1, 8) for s in rng.sample(values, rng.randint(1, len(values)))}
            g = explicit_graph(masses)
            index = HeapIndex(g)
            wanted = sorted(((v, Q(w, sum(masses.values()))) for v, w in masses.items()), key=lambda r: (-r[1], r[0]))
            self.assertEqual(index.total_mass, 1)
            self.assertEqual(index.count, len(wanted))
            self.assertEqual([index.at(i) for i in range(index.count)], wanted)

    def test_transforms_preserve_exact_distribution_and_bind_shared_choice(self):
        source = {b"abc": 3, b"Abc": 2, b"bXy": 1}
        g = explicit_graph(source)
        for transform, map_bytes in ((lambda g: prefixed(g, b"!"), lambda v: b"!" + v),
                                     (swapped_case, lambda v: v.swapcase()),
                                     (lambda g: wrapped_prefix_free(g, b"--"), lambda v: b"--" + v + b"--")):
            new = transform(g)
            index = HeapIndex(new)
            expected = sorted(((map_bytes(v), Q(w, 6)) for v, w in source.items()), key=lambda r: (-r[1], r[0]))
            self.assertEqual(index.total_mass, 1)
            self.assertEqual([index.at(i) for i in range(index.count)], expected)
            for value, score in expected:
                self.assertEqual(probability(new, value), score)
        with self.assertRaisesRegex(ValueError, "prefix-free"):
            wrapped_prefix_free(explicit_graph({b"a": 1, b"ab": 1}), b"!")


if __name__ == "__main__":
    unittest.main()
