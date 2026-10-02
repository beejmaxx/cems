"""Install cems and its recollect compatibility alias for this checkout."""
import argparse
import os
from pathlib import Path
import shlex
import sys
import tempfile

MARKER = "# Managed cems launcher\n"
LEGACY_MARKER = "# Managed recollect launcher for search-prep-lab\n"


def install(prefix):
    directory = Path(prefix).expanduser().resolve() / "bin"
    launchers = [directory / name for name in ("cems", "recollect")]
    source = Path(__file__).resolve().parents[1] / "search.py"
    managed = tuple(("#!/bin/sh\n" + marker).encode() for marker in (MARKER, LEGACY_MARKER))
    # Check both names before writing either; unrelated commands must survive.
    for launcher in launchers:
        if launcher.is_symlink() or (launcher.exists() and
                not launcher.read_bytes().startswith(managed)):
            raise RuntimeError(f"existing unrelated command: {launcher}")
    directory.mkdir(parents=True, exist_ok=True)
    content = ("#!/bin/sh\n" + MARKER + "exec " + shlex.quote(sys.executable) +
               " -B " + shlex.quote(str(source)) + ' "$@"\n')
    for launcher in launchers:
        temporary = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=directory, delete=False) as stream:
                temporary = Path(stream.name)
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            temporary.chmod(0o755)
            temporary.replace(launcher)
        finally:
            if temporary is not None:
                temporary.unlink(missing_ok=True)
    return launchers[0]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prefix", type=Path, default=Path.home() / ".local")
    args = parser.parse_args()
    try:
        command = install(args.prefix)
        print(f"Installed {command} (compatibility alias: {command.with_name('recollect')})")
    except (OSError, RuntimeError) as error:
        parser.exit(1, f"install: {error}\n")
