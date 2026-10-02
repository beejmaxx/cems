#!/usr/bin/env python3
"""Single entrypoint for the source checkout; no installation or automatic work."""
from pathlib import Path
import argparse
import os
import runpy
import sys

LAB = Path(__file__).resolve().parent
for directory in reversed(("src", "tools")):
    sys.path.insert(0, str(LAB / directory))

from layout import source

COMMANDS = {
    "desk": "recipe_desk.py",
    "recipe": "recipe.py",
    "watch": "recipe_preview.py",
    "inspect": "plan_inspect.py",
    "models": "plan_inspect.py",
    "gpu": "gpu_worker.py",
    "gpu-campaign-qualify": "gpu_campaign_qualification.py",
    "worker": "worker.py",
    "runner-v1": "runner.py",
    "checkpoint": "checkpoint_runner.py",
    "model": "model_workflow.py",
    "personal-model": "personal_model.py",
    "history": "history_campaign.py",
    "migrate": "migrate_history.py",
    "luks": "luks1_worker.py",
    "luks-fixture": "luks1_fixture.py",
    "luks-demo": "luks1_demo.py",
    "coffee-story": "coffee_story.py",
    "synthetic-story": "synthetic_story.py",
    "ambiguous-story": "ambiguous_story.py",
    "large-story": "large_story.py",
    "workspace": "workspace.py",
}


def main():
    global_options = argparse.ArgumentParser(add_help=False)
    global_options.add_argument("--workspace", type=Path)
    options, arguments = global_options.parse_known_args(sys.argv[1:])
    if options.workspace:
        os.environ["RECOVERY_WORKSPACE"] = str(options.workspace.expanduser().resolve())
    if not arguments or arguments[0] in {"-h", "--help"}:
        print("Usage: cems [--workspace PATH] COMMAND [arguments]\n")
        print("Start here:\n  desk                      Open the visual model editor\n  models                    List available models and proposals\n"
              "  inspect MODEL --at 1t 2t   Show ranked candidate samples\n"
              "  recipe edit               Edit the private TOML proposal\n"
              "  watch --at 1 1b 1t 100t    Rebuild its preview when saved\n"
              "  workspace use PATH        Remember your private workspace\n")
        print("All commands:\n  " + "\n  ".join(COMMANDS))
        print("\nUse COMMAND --help for its options. No command runs by default.")
        print("Workspace: --workspace PATH, RECOVERY_WORKSPACE, or a saved 'workspace use' selection.")
        return
    command = arguments[0]
    if command not in COMMANDS:
        raise SystemExit("Unknown command: " + command + "; use --help")
    # Saved defaults affect interactive commands only; Workspace itself stays explicit.
    configuring = command == "workspace" and "use" in arguments[1:]
    if not os.environ.get("RECOVERY_WORKSPACE") and not configuring and "--help" not in arguments:
        from cli_settings import saved_workspace
        try:
            location = saved_workspace()
        except (OSError, ValueError) as error:
            raise SystemExit(f"cems: {error}") from None
        if location:
            os.environ["RECOVERY_WORKSPACE"] = location
    if command == "personal-model":
        from workspace import Workspace
        script = Workspace().path("personal_model")
    else:
        script = source(COMMANDS[command])
    if script.parent == LAB / "tools/experiments":
        sys.path.insert(0, str(script.parent))
    extra = ["--list"] if command == "models" else []
    sys.argv = [f"cems {command}", *extra, *arguments[1:]]
    runpy.run_path(str(script), run_name="__main__")


if __name__ == "__main__":
    try:
        main()
        sys.stdout.flush()
    except BrokenPipeError:
        # Piping a preview to head/less must not produce a traceback at shutdown.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    except KeyboardInterrupt:
        raise SystemExit(130) from None
