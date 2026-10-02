"""Explicit private data locations, independent of the software checkout."""
import json
import os
import re
from pathlib import Path

ENVIRONMENT = "RECOVERY_WORKSPACE"
SCHEMA = "private-recovery-workspace-v1"


class Workspace:
    def __init__(self, location=None):
        location = location or os.environ.get(ENVIRONMENT)
        if not location:
            raise RuntimeError("supply --workspace PATH, set RECOVERY_WORKSPACE, or run cems workspace use PATH")
        self.root = Path(location).expanduser().resolve(strict=True)
        self.document = json.loads((self.root / "workspace.json").read_text())
        if self.document.get("schema") != SCHEMA:
            raise ValueError("unsupported workspace schema")

    def resolve(self, relative):
        path = Path(relative)
        if path.is_absolute() or ".." in path.parts or not path.parts:
            raise ValueError("workspace paths must be relative and contained")
        result = self.root / path
        if not result.resolve().is_relative_to(self.root):
            raise ValueError("workspace path escapes its root")
        return result

    def path(self, name):
        try:
            relative = self.document["resources"][name]
        except KeyError:
            raise ValueError("workspace resource is not configured: " + name) from None
        return self.resolve(relative)

    def profiles(self):
        if not self.document.get("profiles") or any(
                not re.fullmatch(r"[A-Za-z0-9_-]+", name) for name in self.document["profiles"]):
            raise ValueError("workspace profile names must be safe file labels")
        return {name: self.resolve(path) for name, path in self.document["profiles"].items()}


def main():
    import argparse
    parser = argparse.ArgumentParser(prog="cems workspace", description=__doc__)
    parser.add_argument("command", choices=("paths", "use"))
    parser.add_argument("location", nargs="?", type=Path, help="workspace to remember with 'use'")
    args = parser.parse_args()
    if args.command == "use":
        if args.location is None:
            parser.error("workspace use requires a path")
        from cli_settings import save_workspace
        try:
            print(f"Workspace: {save_workspace(args.location)}")
        except (OSError, ValueError, RuntimeError) as error:
            parser.exit(2, f"{parser.prog}: {error}\n")
        return
    if args.location is not None:
        parser.error("workspace paths takes no positional path; use --workspace PATH")
    workspace = Workspace()
    print(json.dumps({"workspace": str(workspace.root),
                      "resources": {name: str(workspace.path(name))
                                    for name in workspace.document["resources"]},
                      "profiles": {name: str(path) for name, path in workspace.profiles().items()}}, indent=2))


if __name__ == "__main__":
    main()
