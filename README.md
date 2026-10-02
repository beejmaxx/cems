# CEMS — Cumulative Exact Mixture Search

A weighted finite-language engine for revisable candidate search. CEMS compiles
alternative constructions into compact graphs, sums the exact rational weights
of overlapping explanations, and enumerates distinct strings by descending model
score with bytewise ties. Accepted completed work is subtracted by string
membership when a model changes, preserving the new order of the remaining set.

Python handles model preparation and durable campaign coordination. C++20 handles
ranked plans, symbolic set operations, enumeration, and candidate delivery. The
current adapters support synthetic equality tests and authorized LUKS1 recovery.

Model weights express assumptions; exact arithmetic does not establish their
accuracy. Compilation has explicit graph and integer limits. Lost acknowledgments
can require replaying work. GPU checker limitations are described below.

## Quick start

Requirements: Python 3.11+, Make, and a C++20 compiler. Ordinary tests use generated
fixtures and require no workspace, target material, network access, or GPU.

```sh
make -j2 build
make test test-large
make install
```

The installed command is `recollect` (the existing CLI name). Add `~/.local/bin`
to your PATH. It points to this checkout; rerun installation if the checkout moves.
Use `CXX=g++` on Linux and `PYTHON=/path/to/python3` where needed.

Try the synthetic recipe without a workspace:

```sh
recollect recipe check docs/examples/recipe.toml
recollect desk docs/examples/recipe.toml --draft /tmp/cems-draft.json --output /tmp/cems-previews
```

The local browser editor saves a proposal and shows construction routes, ranked
candidate explanations, and exact pair-membership heatmaps. It performs no checks.
The component editor is still under development and is not part of this release.
See [recipes and previewing](docs/RECIPES.md).

For a single terminal preview:

```sh
mkdir -p /tmp/cems-output
recollect watch docs/examples/recipe.toml --once --at 1 100 --output /tmp/cems-output
recollect --help
```

`inspect PATH.plan --at 1 1b 1t 100t --count 5` jumps directly to ranks. Ranks are
one-based; k/m/b/t/q mean thousand through quadrillion. Exact scores are model
priorities, not recovery odds. Marks outside the plan are identified explicitly.

## Private data

Personal models, seed lists, target images, prepared plans, campaign databases,
and recovery evidence belong in a separate private workspace. They are not
shipped with this repository. Set `RECOVERY_WORKSPACE`, pass `--workspace PATH`,
or run `recollect workspace use PATH` to select one explicitly.

See [workspace setup](docs/WORKSPACE.md). Do not commit a workspace or compiled
plans: those artifacts reveal candidate sets. Project-specific historical
migration adapters and private evidence tests are excluded from this release.
Generic campaign export, audit, and resume remain available through `migrate`.

## Runtime and tests

- `make test`: exact ranking, malformed plans, checkpoint/restart behavior,
  history-preserving revisions, synthetic models, CLI and browser API tests.
- `make test-large`: bounded independent recipe and campaign oracles.
- `make sanitize`: AddressSanitizer and UndefinedBehaviorSanitizer checks.
- `make test-luks1 test-gpu-handoff`: optional crypto and GPU orchestration tests;
  install pkg-config, static OpenSSL libcrypto, PyCryptodome, and qemu-img first.

The GPU bridge pins Hashcat 7.1.2 mode 29511, spools bounded hex wordlists, and
independently confirms reported hits. This mode uses payload entropy and can miss
correct passwords for high-entropy plaintext. Independent hit confirmation does
not fix that false-negative limitation. Actual hardware must pass the synthetic
qualification commands before use. See [LUKS and GPU adapters](docs/ADAPTERS.md).

Source layout: `src/` engine/controllers, `tests/` generated oracles,
`tools/experiments/` synthetic demonstrations, `docs/` guides. Generated binaries
stay under ignored `build/`. See [architecture](docs/ARCHITECTURE.md).

MIT licensed. External runtime dependencies retain their own licenses; Hashcat,
OpenSSL, QEMU, and PyCryptodome are not bundled.
