# Reviewed handoff bundles

Use `shared/scripts/handoff_bundle.py` when preparing the handover described in
[post-migration.md](post-migration.md), including a Mode 2 customer-run handover.
It creates one self-contained ZIP from an explicit reviewed allowlist. It does not
change execution state, dashboard data, approvals, or cutover readiness.

## Review and create

Stop writers while taking the snapshot. Review the actual bytes of each selected
file and the config: **`reviewed_nonsecret: true` is your attestation, not a scanner's
verdict.** There is no automatic secret discovery guarantee. Never add credentials,
database dumps, raw logs, environment files, or an entire engagement by default.
Review evidence and dashboard files for sensitive content and expiring signed URLs
before explicitly adding them. There is no recursive selection or glob expansion.

Save this JSON as `handoff-allowlist.json`, adjusting it to the reviewed files and
known references. Paths are relative to the engagement root; directories are not
accepted. The config itself is not included unless explicitly allowlisted.

```json
{
  "version": 1,
  "reviewed_nonsecret": true,
  "files": [
    "migration-plan.md",
    "authorizations.md",
    "runbooks/cutover.md"
  ],
  "references": [
    {
      "ref": "migration-plan.md",
      "status": "included",
      "path": "migration-plan.md"
    },
    {
      "ref": "authorizations.md §A4b",
      "status": "included",
      "path": "authorizations.md"
    },
    {
      "ref": "runbooks/cutover.md",
      "status": "included",
      "path": "runbooks/cutover.md"
    },
    {
      "ref": "validation-evidence",
      "status": "external",
      "reason": "Held in the customer's evidence system; obtain access from its owner."
    },
    {
      "ref": "raw-database-export",
      "status": "omitted",
      "reason": "Contains production data; intentionally excluded."
    }
  ]
}
```

From the installed skill directory, with an existing private output directory
**outside** the engagement root:

```bash
umask 077
python3 shared/scripts/handoff_bundle.py create \
  --root /work/engagement \
  --config /work/handoff-allowlist.json \
  --output /work/deliverables/handoff-v1.zip
python3 shared/scripts/handoff_bundle.py verify /work/deliverables/handoff-v1.zip
```

All commands are local and stdlib-only. Use Python 3.9+ on POSIX with `O_NOFOLLOW`,
descriptor-relative filesystem operations, and same-directory hard links. Use
physical paths: symlink components (including a system's `/tmp` alias) are rejected.
The output parent must already exist. A new filename is required for each version;
an existing file, directory, or dangling symlink is never overwritten.

## What the manifest proves

The ZIP contains `manifest.json` and exactly `files/<allowlisted-path>` for each
selected file. The generated manifest records each captured file's byte size and
SHA-256, the reference inventory, and separate `included`, `external`, and `omitted`
reference counts. Standard output reports those counts, file/byte totals, the scope
`declared-only`, and a SHA-256 of the whole archive. Keep that archive digest in a
separate trusted handoff record when transfer integrity matters.

- `included` requires a `path` matching an actual allowlisted file. A section
  reference may identify that file, but section existence is not semantically checked.
- `external` means identified but not packaged; `omitted` means deliberately excluded.
  Both require a nonempty `reason`. They are never fetched or silently bundled.
- The `files` list is the exact payload inventory; `references` is the inventory
  declared by the reviewer. Multiple section references can point at one file, so
  reference counts and file counts may differ.

Enumerate known references from the plan, runbooks, and any selected
[dashboard evidence fields](dashboard.md). Classify each as included, external, or
omitted; do not label an unresolved reference included. This helper does not parse
Markdown, HTML, JSON evidence fields, or transitive dependencies to discover missing
references. Even an empty reference list or zero external references means only
**zero declared entries**, never “all engagement evidence is complete.” Unknown schema
fields, including a `complete` claim, are rejected.

## Snapshot, verification, and limits

Every selected file is opened through pinned directories without following symlinks,
then its bytes are captured once into a private staged ZIP. Device/inode, mode, link
count, size, nanosecond modification time, and change time are checked before/after
capture and again across all sources and their paths after archive verification.
Observed edits, growth, truncation, replacements, and path swaps abort publication.
The helper does not lock other writers or create a filesystem-wide atomic snapshot:
pause writers or use a separately prepared filesystem snapshot for that requirement.
Hostile same-user/root writers or filesystems with unreliable metadata are outside
this guarantee, including edits after the final checks.

The verified ZIP is published atomically without replacement, with mode `0400`;
staging uses `0600`. Entries carry private regular-file modes, fixed timestamps, and
sorted inventories. Unchanged content/config produces identical bytes. “Immutable”
here means a read-only, never-updated snapshot: the file owner can still change its
permissions or replace it. No filesystem immutability flag, encryption, digital
signature, secret scan, or approval verification is provided.

`verify` needs only the archive and never extracts it or consults original sources.
It checks CRCs, sizes, SHA-256s, exact membership, duplicate/colliding names, local and
central directory layout, reference accounting, and safe regular-file paths. It
rejects traversal, symlink entries, malformed/truncated archives, extra/missing
members, and trailing data. It accepts this helper's stored, single-disk ZIP format,
not arbitrary third-party ZIPs (compression, ZIP64, comments, and extra fields are
unsupported). Hashes detect corruption, not a malicious rewrite of both payload and
manifest; compare the separately trusted whole-archive digest for that case.

Bounds: 1–1,024 selected files, 64 MiB per file, 256 MiB total payload, 1 MiB each for
config/generated manifest, 4,096 declared references, and 512 UTF-8 bytes per relative
path. Actual open-file limits may be lower. Paths must use NFC-normalized forward
slashes and portable names; absolute paths, `..`, case collisions, Windows device
names, and file/directory collisions are rejected.

Exit codes: `0` verified success, `1` validation/I/O failure, `2` CLI argument error.
Before publication, failures remove staging and leave no output; process termination
may leave a private `.handoff-*.tmp` file. An I/O failure after atomic publication
(for example, directory `fsync`) may leave a verified read-only output despite exit
`1`: inspect and verify it; do not assume the failed invocation made nothing.

Local validation: `python3 -B scripts/test_handoff_bundle.py` from the skill directory.
