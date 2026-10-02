"""Installed CLI and deep rank inspection using generated plans only."""
import argparse
from fractions import Fraction
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest

from fixtures import explicit_graph
from install_cli import install
from layout import LAB
from model_workflow import CORE, build_bundle
from plan_inspect import rank_number
from runner import execute, sha
from swg import write_source
from workspace import SCHEMA


class InspectTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="inspect-cli-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.env = {k: v for k, v in os.environ.items() if k not in {"RECOVERY_WORKSPACE", "PYTHONPATH"}}
        self.env["XDG_CONFIG_HOME"] = str(self.root / "config")
        self.command = install(self.root / "installation with spaces")
        self.ws = self.root / "private workspace"
        self.ws.mkdir()
        self.prepared = self.ws / "prepared-models"
        self.prepared.mkdir()
        self.bundle = self.prepared / "generated"
        self.manifest = build_bundle({"schema": "search-constructions-v1", "hypotheses": [
            {"id": "bits", "weight": 1, "root": {"op": "words", "alphabet": "01", "length": 48}}
        ]}, self.bundle)
        (self.ws / "workspace.json").write_text(json.dumps({"schema": SCHEMA,
            "resources": {}, "profiles": {"generated": "prepared-models/generated"}}))

    def cli(self, *args, okay=True):
        result = subprocess.run([str(self.command), *map(str, args)], cwd=self.root,
            env=self.env, text=True, capture_output=True, timeout=20)
        if okay:
            self.assertEqual(result.returncode, 0, result.stderr)
        else:
            self.assertNotEqual(result.returncode, 0)
            self.assertNotIn("Traceback", result.stderr)
        return result

    def proposal(self, collection="experiment"):
        directory = self.prepared / collection / "example"
        directory.mkdir(parents=True)
        full = directory / "model.plan"
        shutil.copyfile(self.bundle / "model.plan", full)
        remaining = directory / "remaining.plan"
        execute([CORE, "subtract", full, remaining, full, 0, 2])
        (directory.parent / "manifest.json").write_text(json.dumps({
            "status": "passed", "adopted": False, "core_sha256": sha(CORE),
            "history_policy": "generated-test-only", "profiles": [{"profile": "example",
            "full_plan_sha256": sha(full), "remaining_plan_sha256": sha(remaining),
            "model_count": 2**48, "remaining_count": 2**48 - 2}]}))
        return directory

    def test_installed_command_saved_workspace_and_arbitrary_deep_ranks(self):
        settings_before = (self.ws / "workspace.json").read_bytes()
        plans_before = {p.name: sha(p) for p in self.bundle.iterdir() if p.is_file()}
        self.cli("workspace", "use", self.ws)
        listed = json.loads(self.cli("models", "--json").stdout)
        self.assertEqual(listed[0]["model"], "generated")
        ranks = (1, 10**12, 2*10**12, 100*10**12, 200*10**12)
        report = json.loads(self.cli("inspect", "generated", "--at", "1", "1t", "2t", "100t", "200t", "--json").stdout)
        self.assertEqual([w["at"] for w in report["windows"]], list(ranks))
        for window in report["windows"]:
            self.assertEqual(len(window["rows"]), 5)
            for row in window["rows"]:
                self.assertEqual(row["candidate"], f"{row['rank'] - 1:048b}")
                self.assertEqual(Fraction(row["score"]), Fraction(1, 2**48))
        self.assertEqual((self.ws / "workspace.json").read_bytes(), settings_before)
        self.assertEqual({p.name: sha(p) for p in self.bundle.iterdir() if p.is_file()}, plans_before)

    def test_intervals_tail_and_invalid_boundaries(self):
        plan = self.bundle / "model.plan"
        report = json.loads(self.cli("inspect", plan, "--every", "1t", "--from", "2t", "--to", "4t", "--count", 2, "--json").stdout)
        self.assertEqual([w["at"] for w in report["windows"]], [2*10**12, 3*10**12, 4*10**12])
        tail = json.loads(self.cli("inspect", plan, "--at", 2**48 - 1, "--json").stdout)
        self.assertEqual([r["candidate"] for r in tail["windows"][0]["rows"]], ["1"*47 + "0", "1"*48])
        for arguments in (("--at", "0"), ("--at", str(2**48 + 1)), ("--every", "1t"),
                          ("--every", "1", "--to", "1t"), ("--count", "129"),
                          ("--at", "1", "--to", "2t")):
            with self.subTest(arguments=arguments):
                result = self.cli("inspect", plan, *arguments, okay=False)
                self.assertEqual(result.stdout, "")

    def test_proposal_history_views_identity_and_ambiguous_names(self):
        directory = self.proposal()
        self.cli("workspace", "use", self.ws)
        remaining = json.loads(self.cli("inspect", "example", "--count", 1, "--json").stdout)
        full = json.loads(self.cli("inspect", "example", "--full", "--count", 1, "--json").stdout)
        self.assertEqual(remaining["status"], "proposal")
        self.assertEqual(remaining["view"], "remaining")
        self.assertEqual(remaining["windows"][0]["rows"][0]["candidate"], f"{2:048b}")
        self.assertEqual(full["windows"][0]["rows"][0]["candidate"], "0"*48)
        self.proposal("another-experiment")
        error = self.cli("inspect", "example", okay=False)
        self.assertIn("ambiguous", error.stderr)
        self.cli("inspect", "experiment/example", "--count", 1)
        with (directory / "remaining.plan").open("ab") as stream:
            stream.write(b"changed")
        error = self.cli("inspect", "experiment/example", okay=False)
        self.assertIn("manifest", error.stderr)

    def test_workspace_override_and_no_implicit_sibling(self):
        error = self.cli("models", okay=False)
        self.assertIn("workspace use", error.stderr)
        self.cli("workspace", "use", self.ws)
        other = self.root / "other"
        other.mkdir()
        (other / "workspace.json").write_text(json.dumps({"schema": SCHEMA, "resources": {}, "profiles": {}}))
        self.env["RECOVERY_WORKSPACE"] = str(other)
        self.assertEqual(json.loads(self.cli("models", "--json").stdout), [])
        result = json.loads(self.cli("models", "--workspace", self.ws, "--json").stdout)
        self.assertEqual(result[0]["model"], "generated")

    def test_binary_candidates_and_partial_plan_label(self):
        source, plan = self.root / "bytes.swg", self.root / "bytes.plan"
        values = {b"": 5, b"\x1b[31m\n\xff": 4, b" a ": 3, b"a\\x00": 2}
        write_source(source, {"bytes": explicit_graph(values)}, {"bytes": 1})
        execute([CORE, "compile", source, plan])
        human = self.cli("inspect", plan, "--scores").stdout
        self.assertNotIn("\x1b", human)
        self.assertIn('"\\x1b[31m\\n\\xff"', human)
        self.assertIn('" a "', human)
        report = json.loads(self.cli("inspect", plan, "--json").stdout)
        self.assertEqual([bytes.fromhex(r["hex"]) for r in report["windows"][0]["rows"]], list(values))
        partial = self.root / "partial.plan"
        execute([CORE, "prepare", source, partial, 1])
        self.assertIn("prepared prefix", self.cli("inspect", partial).stdout)

    def test_selected_history_preview_is_bound_to_the_full_model(self):
        preview = self.prepared / "saved-preview"
        preview.mkdir()
        full, remaining = self.bundle / "model.plan", preview / "generated-remaining.plan"
        execute([CORE, "subtract", full, remaining, full, 0, 3])
        settings = json.loads((self.ws / "workspace.json").read_text())
        settings["resources"]["preview"] = "prepared-models/saved-preview"
        (self.ws / "workspace.json").write_text(json.dumps(settings))
        self.cli("workspace", "use", self.ws)
        data = {"schema": "history-aware-personal-preview-v1", "core_sha256": sha(CORE),
                "evidence_policy": "generated-test-only", "profiles": [{"profile": "generated",
                "model_plan_sha256": sha(full), "remaining_plan_sha256": sha(remaining),
                "remaining_candidates": 2**48 - 3}]}
        for version in ("v1", "v2"):
            data["schema"] = "history-aware-personal-preview-" + version
            (preview / "manifest.json").write_text(json.dumps(data))
            result = json.loads(self.cli("inspect", "generated", "--count", 1, "--json").stdout)
            self.assertEqual(result["view"], "remaining")
            self.assertEqual(result["windows"][0]["rows"][0]["candidate"], f"{3:048b}")
        data["profiles"][0]["model_plan_sha256"] = "0"*64
        (preview / "manifest.json").write_text(json.dumps(data))
        self.assertIn("does not match", self.cli("inspect", "generated", okay=False).stderr)

    def test_exact_suffixes_and_install_collision(self):
        for text, expected in {"1t": 10**12, "2T": 2*10**12, "100t": 10**14, "200t": 2*10**14,
                               "1.5b": 1500000000, "1,000": 1000, "1_000": 1000,
                               "9007199254740993": 9007199254740993}.items():
            self.assertEqual(rank_number(text), expected)
        for text in ("0", "-1", "1.1", "1e12", "nan", "100000000q"):
            with self.assertRaises(argparse.ArgumentTypeError):
                rank_number(text)
        blocked = self.root / "unrelated/bin/recollect"
        blocked.parent.mkdir(parents=True)
        blocked.write_bytes(b"unrelated command")
        with self.assertRaisesRegex(RuntimeError, "unrelated"):
            install(blocked.parent.parent)
        self.assertEqual(blocked.read_bytes(), b"unrelated command")


if __name__ == "__main__":
    unittest.main()
