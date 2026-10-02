# Private workspaces

Keep personal inputs and generated artifacts outside the public checkout. A
workspace can contain `inputs/`, `models/`, `prepared-models/`, `campaigns/`,
`coverage/`, and `archive/`. The location is explicit: `--workspace PATH`, then
`RECOVERY_WORKSPACE`, then a saved `recollect workspace use PATH` setting.

A minimal `workspace.json` example is:

```json
{
  "schema": "private-recovery-workspace-v1",
  "resources": {
    "personal_model": "models/model.py",
    "inputs": "inputs",
    "header": "inputs/header.luks",
    "target": "inputs/target.hash",
    "history": "coverage/accepted"
  },
  "profiles": {"example": "prepared-models/example"}
}
```

Only configure resources required by commands you use. Paths must be relative
and contained within the workspace. A Python frontend is executable code and
must be trusted. The `personal-model` command loads only the explicitly supplied
workspace frontend; no such frontend is shipped with the software.

A desk defaults to `models/RECIPE.toml` and saves a separate `RECIPE.desk.json`.
Its immutable proposal bundles go in `prepared-models/`. Preparation does not
adopt a campaign revision or start checking. The browser shows which saved draft
its preview matches; an edit invalidates earlier exact results until rebuilt.

Models and compiled plans disclose candidate spaces. Keep this workspace in its
own private version control and do not add it as a public Git submodule.
