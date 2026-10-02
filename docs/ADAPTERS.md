# LUKS1 and GPU adapters

Optional build dependencies: pkg-config, a static OpenSSL libcrypto archive,
PyCryptodome, and qemu-img with LUKS support. Build with `make gpu-handoff`;
`make test-luks1 test-gpu-handoff` runs synthetic crypto and orchestration controls.
The ordinary build and tests require none of these dependencies.

The CPU reference checks the LUKS1 stored master-key digest and supports the
specific AES-CBC-ESSIV-SHA256 / PBKDF2-SHA1 profile. It does not support arbitrary
LUKS modes. `luks-fixture` generates public known-password controls with encrypted
payload; it can create a high-entropy control as a diagnostic.

The GPU path is native generation → hex spool → bounded temporary hex wordlist →
Hashcat 7.1.2 mode 29511 → independent digest/QEMU confirmation → durable receipt.
It is not a continuous stdin pipe into Hashcat. Candidates must satisfy the
adapter's printable-ASCII and 1–64-byte bounds. Qualification pins source,
executable, target, and device identities; changes require requalification.

Mode 29511 uses decrypted-payload entropy rather than digest verification. It
can miss the correct password when plaintext entropy is high. Confirming hits
independently protects against false positives but leaves this false-negative
limitation. Negative evidence keeps an explicit checker-policy label.

Inspect the exact qualification options before running:

```sh
recollect gpu qualify --help
recollect gpu-campaign-qualify --help
```

Both commands use generated targets. The campaign qualifier checks saved hits,
negative receipts, process interruptions, reopen/audit, and coverage-preserving
model revisions on the selected host. Passing a Mac control does not establish
Linux GPU performance. No GPU performance figure is assumed by this release.
