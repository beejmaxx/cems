"""User-selected CLI defaults; no model or campaign settings are changed."""
import json
import os
from pathlib import Path
import tempfile


def settings_path():
    base = Path(os.environ.get("XDG_CONFIG_HOME", str(Path.home() / ".config"))).expanduser()
    return base / "recollect" / "config.json"


def saved_workspace():
    path = settings_path()
    if not path.exists():
        return None
    data = json.loads(path.read_text())
    if data.get("schema") != "recollect-cli-v1" or not isinstance(data.get("workspace"), str):
        raise ValueError(f"invalid CLI settings: {path}; reset with recollect workspace use PATH")
    return data["workspace"]


def save_workspace(location):
    from workspace import Workspace
    root = Workspace(location).root
    path = settings_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, delete=False) as stream:
            temporary = Path(stream.name)
            json.dump({"schema": "recollect-cli-v1", "workspace": str(root)}, stream, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return root
