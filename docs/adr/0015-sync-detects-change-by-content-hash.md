# Sync detects change by content hash, in a per-machine state file

Sync has to know which Book Notes changed since last time. We measured the Shelf rather than
reasoning about it, and the measurements decided this.

All 3,136 notes carry the same mtime: the migration rewrote every one of them in a single
pass. That is not a one-off. `clean --rename` would touch 132 files, `autoenrich` touches
whatever it enriches, and a restore from `local-backup` would touch everything. mtime on this
Shelf is not a weak signal, it is an absent one - and it can never detect a deletion, because
a file that is gone has no timestamp to compare.

Reading and hashing all 3,136 notes takes 2.54 seconds against 4.6 MB. With sync running
every thirty minutes there is no performance case for cleverness, so there is no mtime
prefilter and no incremental cache to go stale: every run hashes the whole Shelf.

The state file maps Libris ID to content hash and lives in Libris's config directory (the
same directory as `config.yaml`, honoring `$LIBRIS_CONFIG_DIR` when set). Keyed by ID rather
than path, because paths move - `clean --rename` alone moves 132 of them - and surviving
exactly that is what a Libris ID is for (ADR 0001). Kept outside the Vault, because it is
per-machine state: in the Vault it would sync to the phone and land in every backup to no
purpose. If it is lost, the next run pushes everything and repairs it.

Pushing the whole Shelf every run was the alternative that needed no detection at all. It was
rejected on cost rather than principle: 4.6 MB and roughly 150,000 Cosmos writes a day to
transmit nothing, on a serverless account billed per request.

## Deletions

Because deletions are now detectable, they need a meaning. A Book Note that leaves the Shelf
has its remote document deleted: the remote is a replica and the Shelf is the source of truth
(ADR 0002), so a book that is not in the Library should not be answerable from it. A query for
the dead ID misses, which is what ADR 0003 says a miss should do.

A merged-away ID is not a deletion in this sense. The survivor's document carries its
`superseded_ids` (ADR 0014) and is pushed in the same run, so the remote can still answer for
the old ID without keeping a tombstone of its own.

Sync refuses to propagate deletions when the count is implausible or the Shelf scans empty; it
stops and reports instead. An unmounted drive or a half-finished Obsidian Sync would otherwise
present as 3,136 deletions and empty the remote Library, which is too much to lose to save a
conditional.

## As built (#174)

**The hash is of the document sync sends, not of the file.** The document carries the note's
filename, so a rename with no change to the content still goes up. A file hash would leave the
remote naming the old file. The document also carries the words its title and authors split into
(ADR 0033), so a change to how words are split changes every hash too.

**"Implausible" is more than 20 deletions, and more than a tenth of what the remote holds.**
Twenty covers removing a few books or merging a run of duplicates. On this Shelf a tenth is 313,
so the guard only stops a loss on the scale of a drive going missing. Renames are not deletions:
the ID stays, so the 132 that `clean --rename` moves are 132 pushes. A Shelf that scans empty is
refused whatever the count. `--allow-deletions` lets a refused run through, once someone has checked.

**A note that cannot be read holds back every deletion.** Its Libris ID may be unknown, so it
could be any of the notes that seem to have left. Nothing is deleted until a sync can read every
note, and the run exits non-zero and says why.

**The state file names the account and database it describes.** State recorded for one account
says nothing about another, and trusting it would leave a newly deployed account empty. A remote
with no word counts is treated as holding nothing, whatever the state says: no sync has finished
there (`CountsMissing`).

**A second sync with nothing changed writes nothing.** The word counts are still rebuilt every
run (ADR 0033), but they are compared with the stored document and written only if they differ.
That costs one read rather than a write of 150-190 KiB every half hour.
