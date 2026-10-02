"""Install the recollect command for this checkout and Python interpreter."""
import argparse
import os
from pathlib import Path
import shlex
import sys
import tempfile

MARKER = "# Managed recollect launcher for search-prep-lab\n"


def install(prefix):
    launcher = Path(prefix).expanduser().resolve() / "bin/recollect"
    source = Path(__file__).resolve().parents[1] / "search.py"
    if launcher.is_symlink() or (launcher.exists() and
            not launcher.read_bytes().startswith(("#!/bin/sh\n" + MARKER).encode())):
        raise RuntimeError(f"existing unrelated command: {launcher}")
    launcher.parent.mkdir(parents=True, exist_ok=True)
    content = ("#!/bin/sh\n" + MARKER + "exec " + shlex.quote(sys.executable) +
               " -B " + shlex.quote(str(source)) + ' "$@"\n')
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=launcher.parent, delete=False) as stream:
            temporary = Path(stream.name)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(0o755)
        temporary.replace(launcher)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    return launcher


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    args = parser.parse_args()
    try:
        print(f"Installed {install(args.prefix)}")
    except (OSError, RuntimeError) as error:
        parser.exit(1, f"install: {error}\n")
