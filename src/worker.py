"""New-campaign entrypoint using the checked native reader.

Old controllers/binaries stay frozen. Resume honors each campaign's pinned
identity; this launcher does NOT silently upgrade earlier campaign evidence.
"""
from layout import BUILD
import argparse
import json
from pathlib import Path

from checkpoint_runner import CheckpointCampaign
from runner import HERE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    create = commands.add_parser("create-synthetic")
    create.add_argument("directory", type=Path)
    create.add_argument("plan", type=Path)
    create.add_argument("target", type=Path)
    for name in ("status", "run", "revise", "checkpoint", "audit"):
        cmd = commands.add_parser(name)
        cmd.add_argument("directory", type=Path)
        if name == "run":
            cmd.add_argument("--count", type=int, default=1_000_000)
            cmd.add_argument("--timeout", type=float, default=100)
        if name == "revise":
            cmd.add_argument("plan", type=Path)
    args = parser.parse_args()
    if args.command == "create-synthetic":
        checker = str(BUILD / "synthetic-checker")
        with CheckpointCampaign.create(args.directory, args.plan, args.target,
                [checker, "check", "all", "ok"], [checker, "confirm"], core=BUILD / "prep-workspace") as c:
            result = c.status()
    else:
        with CheckpointCampaign(args.directory) as c:
            if args.command == "run":
                if not 0 < args.timeout <= 3600:
                    parser.error("timeout must be in (0, 3600] seconds")
                result = c.run_batch(args.count, timeout=args.timeout)
            elif args.command == "revise":
                result = c.revise(args.plan)
            else:
                result = getattr(c, args.command)()
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
