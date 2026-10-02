"""Current source/resource paths and exact executable source identities."""
import hashlib
from pathlib import Path

LAB = Path(__file__).resolve().parents[1]
BUILD = LAB / "build/workspace-v1"
from source_paths import SOURCE_PATHS


def source(name):
    """Locate a named source; no recursive search or environment-dependent choice."""
    return LAB / SOURCE_PATHS[name]


def digest(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as stream:
        for block in iter(lambda: stream.read(1024**2), b""):
            h.update(block)
    return h.hexdigest()


def runtime_source_matches(expected, path):
    """Execution requires today's bytes; historical work enters via migration."""
    return expected == digest(path)
