"""Editable-input and generated-secret lifecycle tests; no cryptographic work."""
import copy
from fractions import Fraction as Q
import json
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

from checkpoint_runner import CheckpointCampaign
from model_workflow import build_bundle, expand, load_bundle, preview, revise
from synthetic_story import scenario_inputs, make_secret, exhaustive_oracle, ordered
from test_core import BIN, rows
from test_runner import CHECKER


def literal(value):
    return {"op": "literal", "value": value}


def choices(values):
    return {"op": "choice", "mode": "probability", "options": [
        {"id": str(i), "expr": literal(value), "weight": weight} for i, (value, weight) in enumerate(values)]}


def spec_for(values):
    return {"schema": "search-constructions-v1", "components": {}, "hypotheses": [
        {"id": "single", "weight": 1, "root": choices(values)}]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="editable-model-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.serial = 0

    def bundle(self, spec):
        self.serial += 1
        dest = self.root / f"bundle-{self.serial}"
        build_bundle(spec, dest, core=BIN)
        return dest

    def campaign(self, bundle, *, limit="all", secret=b"outside", name="campaign"):
        target = self.root / (name + ".target")
        target.write_bytes(secret)
        return CheckpointCampaign.create(self.root / name, bundle / "model.plan", target,
            [str(CHECKER), "check", str(limit), "ok"], [str(CHECKER), "confirm"], core=BIN, checkpoint_every=0)

    def test_reused_components_mixture_mass_and_explanation(self):
        spec = spec_for([("x", 2), ("a", 3)])
        spec["components"] = {"common": choices([("x", 2), ("a", 3)])}
        spec["hypotheses"] = [
            {"id": "A", "weight": 1, "root": {"op": "component", "name": "common"}},
            {"id": "B", "weight": 1, "root": choices([("x", 2), ("b", 3)])},
        ]
        bundle = self.bundle(spec)
        result = preview(bundle)
        self.assertEqual([(v["text"], Q(v["probability"])) for v in result["candidates"]],
                         [("x", Q(2, 5)), ("a", Q(3, 10)), ("b", Q(3, 10))])
        self.assertEqual(len(result["candidates"][0]["contributions"]), 2)
        self.assertEqual(sum(Q(v["contribution"]) for v in result["candidates"][0]["contributions"]), Q(2, 5))

    def test_confidence_mass_is_divided_over_outputs(self):
        spec = {"schema": "search-constructions-v1", "hypotheses": [
            {"id": "broad", "weight": 4, "root": {"op": "words", "alphabet": "0123456789", "length": 4}},
            {"id": "narrow", "weight": 1, "root": choices([(chr(65+i), 1) for i in range(10)])}]}
        result = preview(self.bundle(spec))
        self.assertEqual([v["text"] for v in result["candidates"]], list("ABCDEFGHIJ"))
        self.assertTrue(all(Q(v["probability"]) == Q(1, 50) for v in result["candidates"]))

    def test_relocated_bundle_requires_identical_compiler_and_preserves_manifest(self):
        compiler = self.root / "original-compiler"
        shutil.copy2(BIN, compiler)
        bundle = self.root / "portable-bundle"
        build_bundle(spec_for([("a", 2), ("b", 1)]), bundle, core=compiler)
        before = (bundle / "manifest.json").read_bytes()
        moved = self.root / "relocated-compiler"
        compiler.rename(moved)
        with patch("model_workflow.CORE", moved):
            self.assertEqual([row["text"] for row in preview(bundle)["candidates"]], ["a", "b"])
        self.assertEqual((bundle / "manifest.json").read_bytes(), before)
        moved.write_bytes(b"different compiler")
        with self.assertRaisesRegex(RuntimeError, "explicit rebuild"):
            load_bundle(bundle, core=moved)

    def test_source_identities_work_through_a_directory_alias(self):
        from emitter_v1 import automaton, model
        alias = self.root / "source-alias"
        alias.symlink_to(Path(automaton.__file__).resolve().parent, target_is_directory=True)
        with patch.object(automaton, "__file__", str(alias / "automaton.py")), \
                patch.object(model, "__file__", str(alias / "model.py")):
            bundle = self.bundle(spec_for([("generated", 1)]))
        manifest, _ = load_bundle(bundle)
        self.assertIn("src/emitter_v1/automaton.py", manifest["source_identities"])
        self.assertIn("src/emitter_v1/model.py", manifest["source_identities"])

    def test_shared_binding_differs_from_independent_component_reuse(self):
        selection = {"op": "component", "name": "separator"}
        output = {"op": "concat", "parts": [{"op": "ref", "name": "s"}, literal("q"), {"op": "ref", "name": "s"}]}
        spec = {"schema": "search-constructions-v1", "components": {"separator": choices([("#", 1), ("-", 1)])},
                "hypotheses": [{"id": "shared", "weight": 1,
                    "root": {"op": "let", "bindings": [{"name": "s", "expr": selection}], "output": output}}]}
        first = self.bundle(spec)
        self.assertEqual(rows(first / "model.plan", 0, 2), [(b"#q#", Q(1, 2)), (b"-q-", Q(1, 2))])
        spec["hypotheses"][0]["root"] = {"op": "concat", "parts": [selection, literal("q"), selection]}
        second = self.bundle(spec)
        self.assertEqual(rows(second / "model.plan", 0, 4), [(v, Q(1, 4)) for v in (b"#q#", b"#q-", b"-q#", b"-q-")])

    def test_preview_is_read_only_replan_promotes_unchecked_and_keeps_partial_tail(self):
        first = self.bundle(spec_for([("a", 4), ("b", 3), ("c", 2), ("d", 1)]))
        later = self.bundle(spec_for([("a", 9), ("d", 8), ("z", 7), ("b", 6), ("c", 5)]))
        with self.campaign(first, limit=1) as c:
            answer = c.run_batch(4)
            self.assertEqual((answer["committed_negative"], answer["uncredited_tail"]), (1, 3))
            old_state, old_head = copy.deepcopy(c.state), c.head
            c.checkpoint()
        result = preview(later, campaign=self.root / "campaign")
        self.assertEqual([v["text"] for v in result["candidates"]], ["d", "z", "b", "c"])
        self.assertEqual(result["excluded_completed_strings"], 1)
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertEqual((c.state, c.head), (old_state, old_head))
        revise(later, self.root / "campaign")
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertEqual(c.state["cursor"], 0)
            self.assertEqual(rows(c.blob(c.state["plan"]), 0, 4),
                [(b"d", Q(8, 35)), (b"z", Q(7, 35)), (b"b", Q(6, 35)), (b"c", Q(5, 35))])
            c.run_batch(4)
            self.assertEqual(c.state["checked_negative"], 2)
            self.assertTrue(c.audit()["verified"])

    def test_lost_ack_is_not_coverage_and_durable_ack_is_not_repeated(self):
        first = self.bundle(spec_for([("a", 3), ("b", 2), ("c", 1)]))
        with self.campaign(first) as c:
            with self.assertRaisesRegex(RuntimeError, "before commit"):
                c.run_batch(1, fault="before_commit")
            pending = c.state["pending"]
        view = preview(first, campaign=self.root / "campaign")
        self.assertEqual(view["acknowledged_negatives"], 0)
        self.assertEqual(view["pending_not_credited"], pending)
        self.assertEqual(view["candidates"][0]["text"], "a")
        with CheckpointCampaign(self.root / "campaign") as c:
            self.assertEqual(c.run_batch(1)["job"], pending)  # Safe repeated physical work, not false coverage.
            with self.assertRaisesRegex(RuntimeError, "after durable"):
                c.run_batch(1, fault="after_commit")
        self.assertEqual([v["text"] for v in preview(first, campaign=self.root / "campaign")["candidates"]], ["c"])

    def test_mass_preserving_branch_split_does_not_change_ranking(self):
        spec = spec_for([("a", 7), ("b", 3)])
        first = self.bundle(spec)
        branch = spec["hypotheses"][0]
        spec["hypotheses"] = [dict(branch, id="split-1", weight="1/4"), dict(branch, id="split-2", weight="3/4")]
        second = self.bundle(spec)
        self.assertEqual(rows(first / "model.plan", 0, 2), rows(second / "model.plan", 0, 2))

    def test_invalid_and_changed_inputs_fail_closed(self):
        spec = spec_for([("a", 1)])
        for change in (lambda s: s.update(components={"a": {"op": "component", "name": "a"}}),
                       lambda s: s["hypotheses"][0].update(weight=0.5),
                       lambda s: s["hypotheses"].append(copy.deepcopy(s["hypotheses"][0])),
                       lambda s: s["hypotheses"][0].update(root={"op": "component", "name": "missing"})):
            invalid = copy.deepcopy(spec)
            change(invalid)
            with self.assertRaises((RuntimeError, ValueError)):
                expand(invalid)
        bundle = self.bundle(spec)
        with self.assertRaisesRegex(RuntimeError, "destination exists"):
            build_bundle(spec, bundle, core=BIN)
        (bundle / "inputs.json").write_text("{}")
        with self.assertRaisesRegex(RuntimeError, "bundle changed"):
            preview(bundle)

    def test_storage_preflight_and_unsupported_dependency_leave_no_valid_bundle(self):
        spec = spec_for([("a", 1)])
        destination = self.root / "too-small"
        with self.assertRaisesRegex(RuntimeError, "storage budget"):
            build_bundle(spec, destination, core=BIN, storage_limit=1024)
        self.assertFalse(destination.exists())
        with patch("model_workflow.shutil.disk_usage") as usage:
            usage.return_value.free = 0
            with self.assertRaisesRegex(RuntimeError, "disk reserve"):
                build_bundle(spec, destination, core=BIN)
        self.assertFalse(destination.exists())
        spec["hypotheses"][0]["root"] = {"op": "let", "bindings": [{"name": "x", "expr":
            {"op": "words", "alphabet": "0123456789", "length": 14}}],
            "output": {"op": "concat", "parts": [{"op": "ref", "name": "x"}, {"op": "ref", "name": "x"}]}}
        with self.assertRaisesRegex(ValueError, "expansion"):
            build_bundle(spec, destination, core=BIN)
        self.assertFalse((destination / "manifest.json").exists())

    def test_large_independent_domain_is_symbolic_not_a_candidate_file(self):
        spec = {"schema": "search-constructions-v1", "hypotheses": [{"id": "digits", "weight": 1,
            "root": {"op": "words", "alphabet": "0123456789", "length": 14}}]}
        bundle = self.bundle(spec)
        manifest, _ = load_bundle(bundle)
        self.assertEqual(int(manifest["native_compile"]["candidates"]), 10**14)
        self.assertLess(sum(p.stat().st_size for p in bundle.iterdir()), 20_000)
        self.assertEqual(rows(bundle / "model.plan", 10**14-1, 1), [(b"99999999999999", Q(1, 10**14))])


class GeneratedCohortTests(unittest.TestCase):
    def test_twelve_generated_secrets_same_clues_revisions_and_native_checker(self):
        with tempfile.TemporaryDirectory(prefix="generated-cohort-") as temporary:
            root = Path(temporary)
            models = []
            oracles = []
            for stage in range(3):
                dest = root / f"model-{stage}"
                build_bundle(scenario_inputs(stage), dest, core=BIN)
                models.append(dest)
                oracles.append(exhaustive_oracle(stage))
            recovered = set()
            for seed in range(12):
                with self.subTest(seed=seed):
                    secret, _ = make_secret(seed)
                    target = root / f"target-{seed}"
                    target.write_bytes(secret)
                    campaign = root / f"campaign-{seed}"
                    done = set()
                    self.assertNotIn(secret, oracles[0])
                    with CheckpointCampaign.create(campaign, models[0] / "model.plan", target,
                            [str(CHECKER), "check", "all", "ok"], [str(CHECKER), "confirm"], core=BIN,
                            checkpoint_every=0):
                        pass
                    for stage in range(3):
                        eligible = ordered(oracles[stage], done)
                        with CheckpointCampaign(campaign, checkpoint_every=0) as c:
                            if stage:
                                c.revise(models[stage] / "model.plan")
                            self.assertEqual(rows(c.blob(c.state["plan"]), 0, 12), eligible[:12])
                            answer = c.run_batch(17 if stage < 2 else 10_000)
                            evaluated = {v for v, _ in eligible[:answer["committed_negative"]]}
                            self.assertFalse(evaluated & done)
                            self.assertNotIn(secret, evaluated)
                            done.update(evaluated)
                            self.assertEqual(c.state["checked_negative"], len(done))
                            self.assertEqual(answer["status"], "hit" if stage == 2 else "negative")
                            if stage == 2:
                                self.assertEqual(bytes.fromhex(c.state["hit"]["hex"]), secret)
                                c.checkpoint()
                                self.assertTrue(c.audit()["verified"])
                                recovered.add(secret)
                    with CheckpointCampaign(campaign) as c:
                        self.assertEqual(bytes.fromhex(c.state["hit"]["hex"]), secret)
            self.assertEqual(len(recovered), 12)


if __name__ == "__main__":
    unittest.main()
