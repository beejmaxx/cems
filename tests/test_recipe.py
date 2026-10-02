"""Recipe semantics and live reload, using small public generated inputs only."""
from collections import defaultdict
import copy
from fractions import Fraction as Q
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest

from emitter_v1 import automaton, model
from layout import LAB
from model_workflow import expand
from recipe import compile_recipe, read_recipe


def fixture():
    return {"schema": "recollect-recipe-v1", "status": "proposal", "name": "public fixture",
        "families": {"one": 1},
        "case": {"lower": 8, "title": 4, "upper": 2, "mixed": 1, "mixed_budget": "per-pattern"},
        "lengths": {"short": {"2": 1, "4": 1}},
        "slots": {"separator": {"options": [{"value": "-", "weight": 3}, {"value": "_", "weight": 1}]},
                  "a": {"options": [{"value": "ab", "weight": 1}]},
                  "b": {"options": [{"value": "cd", "weight": 1}]}},
        "templates": [{"id": "pair", "family": "one", "weight": 1,
                       "pattern": "{separator}{chunk1}{separator}{chunk2}", "chunks": ["a", "b"],
                       "lengths": "equal", "length_table": "short", "case": "literal", "separators": "shared"}]}


def distribution(recipe):
    branches = expand(compile_recipe(recipe))
    result = defaultdict(Q)
    total = sum(Q(b["weight"]) for b in branches)
    for b in branches:
        index = automaton.compile_probability(model.model(b["root"]))
        assert index.count < 20000
        for i in range(index.count):
            value, p = index.at(i)
            result[value.decode()] += p * Q(b["weight"]) / total
    assert sum(result.values()) == 1
    return dict(result)


class RecipeTests(unittest.TestCase):
    def test_default_relationships_are_explicit_and_overridable(self):
        recipe = fixture(); template = recipe['templates'][0]
        del template['separators']; del template['case']
        recipe['relationships'] = {'separators':'independent','equal_case':'independent-style','other_case':'literal'}
        result=distribution(recipe)
        self.assertEqual(result['-AB_cd'], Q(3,16)*Q(2,14)*Q(8,14))
        template['case']='literal'; template['separators']='shared'
        self.assertEqual(distribution(recipe), {'-ab-cd':Q(3,4),'_ab_cd':Q(1,4)})

    def test_shared_and_independent_boundaries_are_different_probabilities(self):
        recipe = fixture()
        self.assertEqual(distribution(recipe), {"-ab-cd": Q(3,4), "_ab_cd": Q(1,4)})
        recipe["templates"][0]["separators"] = "independent"
        self.assertEqual(distribution(recipe), {"-ab-cd": Q(9,16), "-ab_cd": Q(3,16), "_ab-cd": Q(3,16), "_ab_cd": Q(1,16)})
        recipe["slots"]["separator"]["options"][1]["value"] = ""
        self.assertEqual(distribution(recipe)["abcd"], Q(1,16))

    def test_shared_and_independent_case_patterns(self):
        recipe = fixture(); template = recipe["templates"][0]
        template["pattern"] = "{chunk1}{chunk2}"
        template["case"] = "shared-pattern"
        shared = distribution(recipe)
        self.assertEqual(len(shared), 4)
        self.assertEqual(shared["abcd"], Q(8,15))
        self.assertNotIn("ABcd", shared)
        template["case"] = "independent-pattern"
        independent = distribution(recipe)
        self.assertEqual(len(independent), 16)
        self.assertEqual(independent["ABcd"], Q(2*8,15**2))
        template["case"] = "independent-style"
        self.assertEqual(len(distribution(recipe)), 9)

    def test_mixed_case_total_budget_is_not_per_variant(self):
        recipe = fixture(); template = recipe["templates"][0]
        recipe["slots"]["a"]["options"][0]["value"] = "abcd"
        recipe["slots"]["b"]["options"][0]["value"] = "efgh"
        template.update(pattern="{chunk1}{chunk2}", case="shared-pattern")
        ordinary = {"abcdefgh", "AbcdEfgh", "ABCDEFGH"}
        result = distribution(recipe)
        self.assertEqual(sum(p for v,p in result.items() if v not in ordinary), Q(13,27))
        recipe["case"]["mixed_budget"] = "family"
        result = distribution(recipe)
        self.assertEqual(sum(p for v,p in result.items() if v not in ordinary), Q(1,15))
        self.assertEqual(result["abcdefgh"], Q(8,15))

    def test_swapped_chunks_and_deletion_positions_aggregate(self):
        recipe = fixture(); template = recipe["templates"][0]
        template.update(pattern="{chunk2}{chunk1}", lengths="any")
        self.assertEqual(distribution(recipe), {"cdab": Q(1)})
        recipe["slots"]["a"] = {"options": [{"value": "aa", "weight": 1}],
            "transforms": [{"op": "identity", "weight": 3}, {"op": "delete-one", "weight": 1}]}
        self.assertEqual(distribution(recipe), {"cdaa": Q(3,4), "cda": Q(1,4)})

    def test_overlapping_transformations_sum(self):
        recipe = fixture(); template = recipe["templates"][0]
        template.update(chunks=["a"], pattern="{chunk1}", lengths="any")
        recipe["slots"]["a"] = {"options": [{"value":"ab","weight":3},{"value":"ba","weight":1}],
            "transforms":[{"op":"identity","weight":3},{"op":"reverse","weight":1}]}
        self.assertEqual(distribution(recipe), {"ab": Q(5,8), "ba": Q(3,8)})

    def test_family_budget_does_not_grow_when_lengths_are_added(self):
        recipe = fixture(); template = recipe["templates"][0]
        recipe["families"] = {"one": 3, "broad": 1}
        recipe["slots"]["letters"] = {"alphabet":"xy"}
        other = dict(template, id="other", family="broad", chunks=["letters"], pattern="{chunk1}")
        recipe["templates"].append(other)
        result = distribution(recipe)
        self.assertEqual(result["-ab-cd"], Q(9,16))
        self.assertEqual(sum(p for v,p in result.items() if set(v)<=set("xy")), Q(1,4))
        recipe["lengths"]["short"]["3"] = 1
        self.assertEqual(distribution(recipe)["-ab-cd"], Q(9,16))

    def test_unequal_broad_pairs_share_one_family_budget(self):
        recipe = fixture(); template = recipe["templates"][0]
        recipe["slots"]["a"] = {"alphabet":"ab"}; recipe["slots"]["b"] = {"alphabet":"cd"}
        recipe["lengths"]["short"] = {"1":1,"2":1}
        template.update(pattern="{chunk1}{chunk2}", lengths="unequal")
        result = distribution(recipe)
        self.assertEqual(len(result), 16)
        self.assertTrue(all(p == Q(1,16) for p in result.values()))

    def test_invalid_settings_fail_instead_of_silently_ignoring(self):
        bad = []
        r=fixture();r['templates'][0]['separators']='magic';bad.append(r)
        r=fixture();r['case']['mixed_budget']='automatic';bad.append(r)
        r=fixture();r['families']['one']=0;bad.append(r)
        r=fixture();r['slots']['a']['typo_simulation']=True;bad.append(r)
        r=fixture();r['templates'][0]['pattern']='{chunk1}{chunk1}';bad.append(r)
        r=fixture();r['families']['one']=0.5;bad.append(r)
        r=fixture();r['slots']['unbounded']={'alphabet':'ab'};r['templates'][0]['pattern']+='{unbounded}';bad.append(r)
        for recipe in bad:
            with self.subTest(recipe=recipe), self.assertRaises((ValueError, KeyError)):
                compile_recipe(recipe)

    def test_watch_reloads_and_recovers_from_invalid_toml(self):
        with tempfile.TemporaryDirectory(prefix="recipe-watch-test-") as temp:
            root = Path(temp); recipe = root/'recipe.toml'; output=root/'previews'; log=root/'watch.log'
            original=(LAB/'docs/examples/recipe.toml').read_text();recipe.write_text(original)
            env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','RECOVERY_WORKSPACE'}}
            env['XDG_CONFIG_HOME']=str(root/'config')
            def wait_until(condition):
                end=time.monotonic()+40
                while time.monotonic()<end:
                    if condition():return
                    if process.poll() is not None:self.fail(log.read_text())
                    time.sleep(.1)
                self.fail('watch timeout: '+log.read_text())
            with log.open('wb') as stream:
                process=subprocess.Popen([sys.executable,'-B',str(LAB/'search.py'),'watch',str(recipe),'--output',str(output),'--at','1','100t','--count','2'],
                                         stdout=stream,stderr=subprocess.STDOUT,env=env,start_new_session=True)
                try:
                    wait_until(lambda: 'Saved proposal:' in log.read_text())
                    first=list(output.glob('recipe-*/manifest.json'));self.assertEqual(len(first),1)
                    initial=first[0].read_bytes()
                    recipe.write_text('schema = [invalid')
                    wait_until(lambda:'Recipe error:' in log.read_text())
                    self.assertEqual(first[0].read_bytes(),initial)
                    recipe.write_text(original.replace('remembered = 9','remembered = 2'))
                    wait_until(lambda:log.read_text().count('Saved proposal:')>=2)
                    self.assertEqual(len(list(output.glob('recipe-*/manifest.json'))),2)
                    self.assertIn('Outside this model',log.read_text())
                    self.assertIn('graphs reused',log.read_text())
                    self.assertFalse(list(output.glob('.recipe-building-*')))
                finally:
                    os.killpg(process.pid,signal.SIGINT)
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()

    def test_save_during_preparation_cancels_obsolete_work(self):
        with tempfile.TemporaryDirectory(prefix='recipe-cancel-test-') as temp:
            root=Path(temp);recipe=root/'recipe.toml';output=root/'previews';log=root/'watch.log'
            small=(LAB/'docs/examples/recipe.toml').read_text()
            recipe.write_text(small.replace('"3" = 1','"8" = 1').replace('alphabet = "abc"','alphabet = "abcdefghijklmnopqrstuvwxyz"'))
            env={k:v for k,v in os.environ.items() if k not in {'PYTHONPATH','RECOVERY_WORKSPACE'}}
            env['XDG_CONFIG_HOME']=str(root/'config')
            with log.open('wb') as stream:
                process=subprocess.Popen([sys.executable,'-B',str(LAB/'search.py'),'watch',str(recipe),'--output',str(output),'--at','1','--count','1'],
                                         stdout=stream,stderr=subprocess.STDOUT,env=env,start_new_session=True)
                try:
                    end=time.monotonic()+30
                    while 'Compiling' not in log.read_text() and time.monotonic()<end:
                        self.assertIsNone(process.poll(),log.read_text());time.sleep(.01)
                    self.assertIn('Compiling',log.read_text())
                    recipe.write_text(small)
                    while 'Saved proposal:' not in log.read_text() and time.monotonic()<end:
                        self.assertIsNone(process.poll(),log.read_text());time.sleep(.05)
                    self.assertIn('discarding unfinished preview',log.read_text())
                    manifests=list(output.glob('recipe-*/manifest.json'));self.assertEqual(len(manifests),1,log.read_text())
                    self.assertEqual((manifests[0].parent/'recipe.toml').read_text(),small)
                    self.assertFalse(list(output.glob('.recipe-building-*')))
                finally:
                    os.killpg(process.pid,signal.SIGINT)
                    try:process.wait(timeout=5)
                    except subprocess.TimeoutExpired:os.killpg(process.pid,signal.SIGKILL);process.wait()


if __name__ == "__main__":
    unittest.main()
