# Design a model and preview its ranked guesses

## Visual editor

Run `recollect desk` to open the local tuning desk in your browser. Keep its terminal
running; Ctrl-C stops the server and any unfinished preparation. No additional web
framework or package installation is required.

Three views share the same saved proposal:

- **Subway map** shows the ordered pieces in each construction, including any
  swapped routes. Line width follows that route's model mass **before history
  exclusions**, with a minimum width for visibility. Select a line, then use
  **½ Less weight** or **2× More weight** to change its within-family weight.
  **Add swapped alternative · split 1:1** creates a second construction and splits
  the original route's weight equally. This is an explicit experiment, not a
  recollection; Undo restores the previous draft.
- **Autopsy** shows the top ten candidates and accepts any rank, including `100t`.
  Colored pieces identify source spellings, capitalization, and actual edit
  operations. Shared choices are identified separately. Select another
  contributing branch to see a different construction of the same string. Every
  explanation is checked against the compiled branch probability and their sum
  against the native candidate score. One displayed construction path is not the
  whole score when several paths produce that string.
- **Memory heatmap** selects a construction and shows a grid of its first two
  **named source chunks**, even if their output order is swapped. Source words
  precede case/edit operations. Other parts, including a third chunk, vary under
  that construction. Choose color by count inside a rank budget, first global
  rank, or total candidate count. Click a cell for exact integers and a link to
  its first candidate's autopsy. `1t`, `2t`, `100t`, and `200t` work as budgets.

Heatmap counts are exact intersections with the full or history-subtracted plan
shown in the status area. They are not estimates from sampled candidates, raw
derivation counts, or probabilities. Each cell means “has at least one valid
explanation using this pair in this construction.” A candidate may occupy several
cells after ambiguous edits or segmentation, so adding the cell counts does not
give a unique total. A zero cell can reflect the template's length/case rules or
history exclusions. Colors use a logarithmic scale within the displayed page;
the numbers, not color alone, support comparisons across pages.

Heatmaps page through all source options eight rows/columns at a time and inspect
the compressed graph without enumerating the search space. Views have bounded
work budgets and return a visible error if a query is too complex. They can take
seconds; changing a view never starts a password checker. A uniform alphabet
slot appears as one **Any letters** category rather than pretending to enumerate
all its spellings as axis labels.

**Memories & missing ideas** shows available evidence links and omissions from an
optional `RECIPE.memories.json`. This is a review aid for the starting recipe,
not an automatic import of the old seeds or a live assertion that an edited rule
matches a recollection. Source literal `rdar`, for example, is not labeled a typo
unless the displayed construction actually uses a deletion operation.

If the desk was already running when these views were installed, stop it with
Ctrl-C and rerun `recollect desk` to load the new backend. The saved draft resumes.

The four tabs expose family and capitalization weights, template inclusion and
relationships, words/separators and transformations, and source-length weights.
Sliders use a logarithmic scale; adjacent fields accept exact positive numbers or
fractions. Family percentages describe generative mass, not calibrated recovery
odds or the proportion of checked candidates. Case controls distinguish weight per
mixed pattern from one shared budget for all mixed patterns.

Edits automatically save a separate `models/RECIPE.desk.json` draft and rebuild the
preview. The original `RECIPE.toml`, active model, and accepted history are unchanged.
Reopening the desk resumes that draft. Each successful build is an immutable
`prepared-models/desk-*` proposal, with its effective `recipe.toml` and samples.
Disabled templates/options stay in the editor draft but are omitted from compilation;
unused families are omitted and active family weights renormalize.

The preview starts at `1 1b 100b 1t`. Enter other ranks such as `1t 2t 100t 200t` in
**Set ranks**. History is subtracted using the recipe's existing pinned policy.
Builds are asynchronous; changing the draft cancels obsolete preparation. While a
build is pending or fails, the last successful samples are labelled as an older
preview. The terminal watcher can compare previous coordinate samples. Undo restores
the preceding edit in the current browser session. Undo history is not persisted.

The desk binds only to `127.0.0.1`, uses a session token, and serves no external assets.
Use the printed URL, or let the command open the browser. `--no-open`, `--port`,
`--draft`, and `--output` are available; `recollect desk --help` lists them. For a
standalone public example:

```sh
recollect desk docs/examples/recipe.toml --draft /tmp/example-desk.json --output /tmp/example-previews
```

This editor exposes the current recipe schema; it does not automatically import the
external seed rules or establish probabilities from recollections. Some
structural combinations exceed native limits or are unsupported by the recipe
compiler. These produce a visible build error, never a silently truncated preview.
Preparation is not instantaneous: compilation can take seconds to tens of seconds, depending on model complexity.

## Text editor and terminal watcher

Run these in two terminal panes:

```sh
recollect recipe edit
recollect watch --at 1 1m 1b 100b 1t 2t 100t 200t --count 5
```

The default file is `models/RECIPE.toml` in your selected workspace. `recipe edit`
uses `$VISUAL`, then `$EDITOR`, then `nvim`. `recollect recipe path` prints its
location. Supply a file to either command to review a different recipe.

Saving the TOML starts an offline compilation. The watcher prints progress and
the new samples only after a successful build. It cancels obsolete preparation
when another save arrives. Invalid TOML or a compiler resource limit leaves the
last successful preview intact. Ctrl-C stops the watcher and its preparation.
Compilation time depends on the graph; it is not guaranteed to be instant.

The output identifies the main contributing explanation for each sampled string.
After an edit, changed windows also show what occupied those same ranks in the
preceding successful preview. These are coordinate comparisons, not inverse-rank
lookups for each old candidate. Ranks outside the new model are labeled explicitly.

Every successful result is an immutable `recipe-…` proposal in `prepared-models/`,
visible in `recollect models`. It contains the recipe, construction inputs,
compiled graphs, full plan, optional remaining plan and sampled explanations.
Unchanged graphs are reused. No command here starts a checker, adopts a campaign
revision, or replaces the selected personal model.

For one preview, validation, or a readable weight summary:

```sh
recollect watch --once --at 1 1t 100t
recollect recipe check
recollect recipe show
```

## What the recipe says

`[families]` assigns relative weights to whole families. Each family's weight is
divided among its templates. A template's weight is divided among its admitted
source lengths. Adding another length does not multiply that family's budget.

`[slots.NAME]` defines a finite list of `{ value, weight }` options or a uniform
lowercase `alphabet`. Weights are positive integers or quoted exact fractions
such as `"1/20"`. They are sensitivity settings, not measured recovery odds.
Evidence and notes do not automatically become weights. Unknown fields fail
validation instead of silently changing or ignoring rules.

`[[templates]]` combines slots and chunks with a readable pattern:

```toml
pattern = "{prefix}{separator}{chunk1}{separator}{chunk2}{last_sep}{suffix}"
chunks = ["first", "second"]
```

Swap `{chunk1}` and `{chunk2}` to reverse their order. Remove boundary placeholders
to propose a construction without them. Add `""`, a space or another literal to
the separator slot to explore a new separator. Every chunk must appear exactly
once; ordinary slot references are fresh choices unless the separator rule below
makes them shared. Pattern literals cannot contain braces in this version.

`{last_sep}` uses a template's `last_separator = { keep = 8, omit = 1 }` choice.
Its default is the same 8:1 setting; these are explicit experimental weights.

## Make relationships explicit

```toml
[relationships]
separators = "shared"
equal_case = "shared-pattern"
other_case = "shared-style"
```

- `shared` selects one separator and reuses it at included boundaries.
- `independent` draws separately at each boundary, including a kept final separator.
- `shared-pattern` reuses an upper/lower pattern across equal-length chunks.
- `independent-pattern` draws a pattern independently for each equal-length chunk.
- `shared-style` shares lower, Title or UPPER style; chunk lengths may differ.
- `independent-style` selects those styles separately for each chunk.
- `literal` preserves source case before explicit transformations.

A template can override the defaults with `separators` or `case`. Source length
rules are `equal`, `unequal` or `any`. Equal/unequal rules name a `[lengths.NAME]`
table. Finite unequal chunks are conditioned on differing lengths; alphabet
chunks use the weighted length table. `any` uses finite chunk lists without
conditioning on length. This initial frontend bounds source lengths to 1–8,
templates to 32 and compiled branches to 32; the existing compiler also retains
its own graph and rational-complexity limits.

## Give mixed case a total budget

```toml
[case]
lower = 8
title = 4
upper = 2
mixed = 1
mixed_budget = "family"
```

For four-letter chunks there are 13 other case patterns. `per-pattern` assigns
each of them weight 1, making their combined share 13/27. `family` splits one
unit among all 13, making their combined share 1/15. This changes a family-level
assumption; it is not a cosmetic display option. Length-one Title and UPPER
coincide, so pattern mode retains one option, as the existing frontend does.

## Transformations have explicit scope

Finite slots can declare weighted operations:

```toml
transforms = [
  { op = "identity", weight = 99 },
  { op = "delete-one", weight = 1 },
]
```

The operation is selected independently for each slot emission. Source lengths
are selected first, capitalization is applied next, then the transformation.
`delete-one` divides its weight equally among source character positions and
adds duplicate resulting strings. It can therefore produce unequal output
lengths from an equal-source-length template. Do not penalize that same omission
again as a separate unexplained length change.

Other operations are `identity`, `lower`, `title`, `upper`, `reverse` and
`shift-number-row` (US digits/shifted number-row symbols). A list of operations
is a weighted choice, not a pipeline. Generic alphabet slots do not yet support
these operations; use finite word options for deletion experiments.

## History and reproducibility

An optional `[history]` binds a workspace coverage resource, its exact metadata
hash and its checker policy. The default display then samples the remaining
plan. `--full` shows ranks before these exclusions. A changed history snapshot
requires an explicit recipe update; it is never silently substituted.

An optional `[source]` pins the comparison model, input hash, plan hash and native
engine. Reusable graphs must match expanded construction identities. A recipe
includes only the rules supplied by its author; importing an external seed corpus
requires explicit translation and review.

A small public example works without a workspace:

```sh
recollect recipe check docs/examples/recipe.toml
recollect watch docs/examples/recipe.toml --once --at 1 100 --output /path/to/previews
```

For lower-level tooling, `recollect recipe export FILE --output NEW.json` emits
the existing construction format. Personal material stays in the private
workspace; the software, example and tests use public synthetic inputs.
