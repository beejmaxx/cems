"""Cross-check symbolic set operations against generated finite sets."""
import json
import os
from pathlib import Path
import tempfile
import unittest
from fixtures import explicit_graph, write_source
from layout import BUILD
from model_workflow import CORE as PREP
from runner import execute
GEOMETRY = Path(os.environ.get("LEGACY_GEOMETRY", BUILD / "set-geometry"))
def command(*args): return json.loads(execute(args))
def values(plan):
    n=int(command(PREP,"describe",plan)["candidates"])
    return [bytes.fromhex(row.split()[0]) for row in execute([PREP,"inspect",plan,0,n]).decode().splitlines()]
class SetTests(unittest.TestCase):
    def test_union_selection_intersections_match_explicit_oracle(self):
        with tempfile.TemporaryDirectory(prefix="legacy-set-ops-") as temp:
            root = Path(temp)
            distributions = ({b"a": 8, b"ab": 4, b"z": 1}, {b"ab": 1, b"y": 1})
            plans = []
            for i, distribution in enumerate(distributions):
                source, plan = root/f"{i}.swg", root/f"{i}.plan"
                write_source(source, {"test": explicit_graph(distribution)}, {"test": 1})
                command(PREP, "compile", source, plan)
                plans.append(plan)
            selected, union = root/"selected.plan", root/"union.plan"
            command(GEOMETRY, "select", plans[0], 0, 2, selected)
            self.assertEqual(values(selected), [b"a", b"ab"])
            command(GEOMETRY, "union", union, *plans)
            self.assertEqual(values(union), [b"a", b"ab", b"y", b"z"])
            result = command(GEOMETRY, "overlap", union, selected, plans[1])
            self.assertEqual(result, {"base_count": 4, "intersections": [2, 2]})
