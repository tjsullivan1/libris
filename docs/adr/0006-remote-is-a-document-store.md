# The remote replica is a document store

ADR 0005 requires the remote to carry frontmatter keys Libris does not model, verbatim and
unmodified. That requirement chooses the storage shape: in a relational store, "arbitrary
keys I have never seen" becomes a JSON column that cannot be queried usefully, which pays
the flexibility penalty without buying the flexibility.

Cosmos DB serverless, with two containers. `books` holds one document per Book Note —
modelled frontmatter, passthrough keys, and the note body — partitioned by Libris ID.
`intents` holds pending and applied Intents, so applied ones remain as a record of what each
Surface did.

The whole Book Note goes up, body included. The entire Shelf is about 4.3 MB of content, so
storing the descriptions costs nothing and leaves a published view able to show them.

Access is via managed identity. No connection string is issued, so none can leak.
