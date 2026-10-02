"""Cross-check views against explicit enumeration, never personal material."""
from collections import defaultdict
from fractions import Fraction as Q
import copy
import json
from pathlib import Path
import tempfile
import unittest

from emitter_v1 import automaton, model
from model_workflow import CORE, expand
from recipe import compile_recipe
from recipe_desk import recipe_bytes
from recipe_preview import build_proposal
from recipe_views import Views
from runner import execute, sha
from test_recipe import fixture


class ViewTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def build(self, recipe):
        path = self.root / 'bundle'
        build_proposal(recipe, recipe_bytes(recipe), path)
        return path, Views(path)

    def overlapping(self):
        recipe = fixture()
        recipe['slots']['a'] = {'options': [{'value': 'aa', 'weight': 2}, {'value': 'a', 'weight': 1}],
                               'transforms': [{'op': 'identity', 'weight': 3}, {'op': 'delete-one', 'weight': 1}]}
        recipe['slots']['b'] = {'options': [{'value': 'b', 'weight': 1}, {'value': 'bb', 'weight': 1}],
                               'transforms': [{'op': 'identity', 'weight': 1}, {'op': 'reverse', 'weight': 1}]}
        recipe['slots']['separator']['options'] = [{'value': '', 'weight': 1}, {'value': '-', 'weight': 1}]
        recipe['templates'][0].update(lengths='any', pattern='{chunk2}{separator}{chunk1}', weight=1)
        duplicate = copy.deepcopy(recipe['templates'][0]); duplicate.update(id='duplicate', weight=3)
        recipe['templates'].append(duplicate)
        distribution, members = defaultdict(Q), defaultdict(set)
        # Independent explicit construction, including collisions from repeated
        # characters, reversal, deletion and duplicate high-level explanations.
        for source, weight in [('aa', Q(2, 3)), ('a', Q(1, 3))]:
            edits = [(source, Q(3, 4))] + [(source[:i]+source[i+1:], Q(1, 4*len(source))) for i in range(len(source))]
            for second in ['b', 'bb']:
                for output, p in edits:
                    for separator in ['', '-']:
                        candidate = second + separator + output
                        distribution[candidate] += weight*p/4
                        members[source, second].add(candidate)
        ranked = sorted(distribution, key=lambda value: (-distribution[value], value.encode()))
        self.assertEqual(sum(distribution.values()), 1)
        return recipe, distribution, members, ranked

    def test_autopsy_sums_overlaps_and_reconstructs_swapped_transformed_pieces(self):
        recipe, distribution, _, ranked = self.overlapping()
        _, views = self.build(recipe)
        saw_ambiguous = saw_deleted = False
        for rank, expected in enumerate(ranked, 1):
            result = views.autopsy(rank)
            self.assertEqual(bytes.fromhex(result['hex']).decode(), expected)
            self.assertEqual(Q(result['score']), distribution[expected])
            self.assertEqual(sum(Q(p['contribution']) for p in result['contributions']), distribution[expected])
            self.assertEqual(len(result['contributions']), 2)
            for contribution in result['contributions']:
                trace = contribution['trace']
                self.assertEqual(Q(trace['probability']), distribution[expected])
                self.assertEqual(''.join(p['text'] for p in trace['pieces']), expected)
                self.assertEqual([p['role'] for p in trace['pieces']], ['chunk2', 'separator', 'chunk1'])
                saw_ambiguous |= trace['other_paths']
                saw_deleted |= any(f.get('transform') == 'delete-one' for p in trace['pieces'] for f in p['facts'])
        self.assertTrue(saw_ambiguous)
        self.assertTrue(saw_deleted)

    def test_heatmap_unique_counts_first_ranks_and_partial_score_bands(self):
        recipe, _, members, ranked = self.overlapping()
        _, views = self.build(recipe)
        for budget in [1, 3, 7, len(ranked), len(ranked)+10]:
            result = views.heatmap('pair', budget)
            for cell in result['cells']:
                candidates = members[cell['row'], cell['column']]
                expected_ranks = [i+1 for i, value in enumerate(ranked) if value in candidates]
                self.assertEqual(int(cell['count']), len(candidates))
                self.assertEqual(int(cell['in_budget']), sum(r <= budget for r in expected_ranks))
                self.assertEqual(cell['first_rank'], str(min(expected_ranks)))
        # The two source spellings overlap after deletion. This sum is
        # deliberately larger than the count of unique ranked candidates.
        self.assertGreater(sum(int(c['count']) for c in result['cells']), len(ranked))

    def test_heatmap_uses_remaining_plan_and_its_ranks(self):
        recipe, _, members, ranked = self.overlapping()
        path, _ = self.build(recipe)
        execute([CORE, 'subtract', path/'model.plan', path/'remaining.plan', path/'model.plan', 0, 3])
        manifest = json.loads((path/'manifest.json').read_text())
        manifest['review']['remaining'] = {'sha256': sha(path/'remaining.plan'), 'count': len(ranked)-3}
        (path/'manifest.json').write_text(json.dumps(manifest))
        views = Views(path)
        result = views.heatmap('pair', 4)
        self.assertEqual(result['view'], 'remaining')
        for cell in result['cells']:
            ranks = [i+1 for i, value in enumerate(ranked[3:]) if value in members[cell['row'], cell['column']]]
            self.assertEqual(int(cell['count']), len(ranks))
            self.assertEqual(cell['first_rank'], str(min(ranks)) if ranks else None)
            self.assertEqual(int(cell['in_budget']), sum(r<=4 for r in ranks))
        self.assertEqual(bytes.fromhex(views.autopsy(1)['hex']).decode(), ranked[3])

    def test_symbolic_heatmap_above_javascript_safe_integer(self):
        recipe = fixture()
        recipe['slots']['a'] = recipe['slots']['b'] = {'alphabet': 'abcdefghijklmnopqrstuvwxyz'}
        recipe['slots']['separator']['options'] = [{'value': '', 'weight': 1}]
        recipe['lengths']['short'] = {'6': 1}
        recipe['templates'][0]['pattern'] = '{chunk1}{chunk2}'
        _, views = self.build(recipe)
        budget = 10**16 + 37
        cell = views.heatmap('pair', budget)['cells'][0]
        self.assertEqual(cell, {'row': None, 'column': None, 'count': str(26**12), 'in_budget': str(budget), 'first_rank': '1'})

    def test_annotated_distribution_preserves_shared_case_and_edit_collisions(self):
        recipe = fixture()
        recipe['slots']['a']['options'] = [{'value': 'aa', 'weight': 1}, {'value': 'ab', 'weight': 2}]
        recipe['slots']['a']['transforms'] = [{'op': 'delete-one', 'weight': 1}, {'op': 'upper', 'weight': 1}]
        recipe['templates'][0].update(case='shared-pattern', pattern='{chunk2}{last_sep}{chunk1}')
        plain = expand(compile_recipe(recipe))
        annotated = expand(compile_recipe(recipe, annotate=True))
        for p, t in zip(plain, annotated):
            p_index = automaton.compile_probability(model.model(p['root']))
            t_index = automaton.compile_probability(model.model(t['root']))
            self.assertEqual(p_index.count, t_index.count)
            self.assertEqual([p_index.at(i) for i in range(p_index.count)], [t_index.at(i) for i in range(t_index.count)])
        _, views = self.build(recipe)
        for rank in range(1, p_index.count+1):
            views.autopsy(rank)  # Must match the non-annotated native score.

    def test_inspection_rejects_altered_plan_or_branch_metadata(self):
        path, views = self.build(fixture())
        with (path/'model.plan').open('ab') as stream:
            stream.write(b'changed')
        with self.assertRaisesRegex(ValueError, 'changed'):
            views.autopsy(1)
        (path/'branches.json').write_text('{}')
        with self.assertRaisesRegex(ValueError, 'metadata changed'):
            Views(path)


if __name__ == '__main__':
    unittest.main()
