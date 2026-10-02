"""Workspace selection and isolation using generated data only."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from layout import LAB
from model_loading import load_module
from workspace import Workspace, SCHEMA


class WorkspaceTests(unittest.TestCase):
    def fixture(self, root, label):
        root.mkdir()
        (root/"models").mkdir()
        (root/"models/model.py").write_text(
            "from pathlib import Path\nimport json\n"
            "VALUE = json.loads(Path(__file__).with_name('config.json').read_text())\n"
            "if __name__ == '__main__': print(VALUE['label'])\n")
        (root/"models/config.json").write_text(json.dumps({"label": label}))
        (root/"workspace.json").write_text(json.dumps({"schema": SCHEMA,
            "resources": {"personal_model": "models/model.py"},
            "profiles": {"example": "prepared-models/example"}}))
        return root

    def test_workspace_is_explicit_and_model_config_stays_beside_frontend(self):
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "--workspace"):
                Workspace()
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp)/"private space", "first")
            ws = Workspace(root)
            self.assertEqual(load_module(ws.path("personal_model")).VALUE, {"label": "first"})
            moved = root.with_name("relocated")
            root.rename(moved)
            self.assertEqual(load_module(Workspace(moved).path("personal_model")).VALUE, {"label": "first"})

    def test_cli_override_beats_environment_from_an_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            first = self.fixture(root/"one", "first")
            second = self.fixture(root/"two", "second")
            env = dict(os.environ, RECOVERY_WORKSPACE=str(first))
            env.pop("PYTHONPATH", None)
            for args in (("--workspace", str(second), "personal-model"),
                         ("personal-model", "--workspace", str(second))):
                result = subprocess.run([sys.executable, "-B", str(LAB/"search.py"), *args],
                    cwd=root, env=env, capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(result.stdout.strip(), "second")

    def test_workspace_paths_cannot_escape(self):
        with tempfile.TemporaryDirectory() as temp:
            root = self.fixture(Path(temp)/"workspace", "example")
            ws = Workspace(root)
            for name in ("../outside", "/absolute", "."):
                with self.assertRaises(ValueError):
                    ws.resolve(name)
            (root/"escape").symlink_to(root.parent, target_is_directory=True)
            with self.assertRaises(ValueError):
                ws.resolve("escape/elsewhere")


if __name__ == "__main__":
    unittest.main()
