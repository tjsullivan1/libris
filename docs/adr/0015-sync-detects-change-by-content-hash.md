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

The state file maps Libris ID to content hash and lives in `~/.config/libris/` beside
`config.yaml`. Keyed by ID rather than path, because paths move - `clean --rename` alone
moves 132 of them - and surviving exactly that is what a Libris ID is for (ADR 0001). Kept
outside the Vault, because it is per-machine state: in the Vault it would sync to the phone
and land in every backup to no purpose. If it is lost, the next run pushes everything and
repairs it.

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
