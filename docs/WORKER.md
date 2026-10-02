# Durable campaign execution

The controller pins an immutable plan, target, checker, and independent confirmer.
It assigns a bounded plan range to the native worker. Candidate delivery is not
completion: only a validated checker receipt grants a contiguous checked prefix.
Unchecked tails remain eligible. A hit must pass independent confirmation.

CheckpointCampaign stores events and checkpoints in SQLite and verifies hashes,
sequence, source identities, and pinned executable identities when reopening.
An unfinished assignment is retried. An acknowledgment lost before durable commit
can cause repeated physical checks; the protocol does not promise exactly-once
execution across crashes. Discovery files can preserve a hit before the journal
commit, but they grant no negative coverage on their own.

HistoryCampaign additionally copies accepted target-bound coverage into the
campaign. Every revision subtracts both the accepted base set and local completed
negatives from the new full plan. It starts at the top of that revised remainder;
an old numeric cursor is not transferred to the new model.

`cems worker --help`, `checkpoint --help`, and `history --help` expose the
local lifecycle. `model preview` is read-only; `model revise` adopts a new model
against the current locked history. Controllers reject altered artifacts and
runtime source mismatches. Storage caps and a free-disk reserve apply.

`migrate export SOURCE DESTINATION` copies acknowledged native campaign history
into portable coverage. `migrate audit` reconstructs its completed set from the
copied receipts, and `migrate resume` creates idle current-format state with an
explicitly accepted policy. These operations do not run a checker. Imported
policies retain their strength; an entropy negative is not upgraded to a digest
negative. Project-specific historical generator adapters are not distributed.

The test suite uses independent finite-set oracles, process loss, partial
receipts, checkpoint/reopen audits, conflicting revisions, and damaged artifacts.
Those checks establish the tested behavior, not universal crash safety.
