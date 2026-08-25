# Libris IDs are minted from each note's `date_added`

ADR 0001 chose ULIDs partly because they sort lexicographically by creation time, which gives
sync a usable cursor. Minting all 3,137 existing IDs at migration time would have given them
near-identical timestamps, making that order meaningless for the entire library we already
hold and useful only for books added afterwards.

So the timestamp component comes from each note's own `date_added` rather than from the
moment the migration runs. The library then sorts in the order it was actually acquired.
Notes with no `date_added` fall back to the migration time; the randomness component keeps
IDs distinct where dates collide, which they will, since `date_added` has day resolution and
books were often added in batches.

`python-ulid` provides the encoding. Hand-rolling Crockford base32 was considered and
rejected: it is a small amount of code but not a small amount of correctness, and the
project can carry a sixth dependency.
