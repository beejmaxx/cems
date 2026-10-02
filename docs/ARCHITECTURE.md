# Exact mixture ranking and revisable coverage

For a finite byte string x, the model assigns score

    P(x) = sum_i w_i P_i(x)

where normalized mixture weights w_i and construction probabilities P_i use
exact rational arithmetic. Different explanations of the same bytes add mass.
The compiler emits distinct strings, ordered by descending score and then bytewise.
The weights are supplied assumptions, not empirically calibrated recovery odds.

The construction frontend supports finite choices, concatenation, bounded word
languages, transformations, conditions, and shared bindings. A shared binding
selects once and reuses its bytes, charging its probability once. Independent
uses draw separately. The compiler builds weighted automata offline; the native
engine stores shared, counted deterministic language graphs and score bands.

For accepted completed language H and new model support S, the eligible language
is S minus H. Subtraction changes membership, preserving original scores and the
relative order of surviving candidates. No enumeration of H is needed when its
symbolic representation fits the resource bounds. Graph complexity can still
explode; large candidate counts alone do not predict memory or compile time.

`src/emitter_v1/`: reusable construction compiler. `model_workflow.py`, `recipe.py`,
`recipe_preview.py`: model inputs and immutable bundles. `src/native/`: native
plans, rank access, set operations, framed delivery and crypto adapters.
`runner.py`, `checkpoint_runner.py`, `history_campaign.py`: receipt authority and
revisions. `coverage_snapshot.py` binds completed sets to targets and policies.
`recipe_desk.py`, `recipe_views.py/js` supply a local proposal editor and inspectors.

Tests use generated models and independent exhaustive oracles at small scale,
plus counted symbolic languages beyond JavaScript's safe-integer range. A public
release contains no personal model, seed corpus, accepted private history, target
material, or original private Git history.
