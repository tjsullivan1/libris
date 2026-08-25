# A write answers with identity, an outcome, and the guarantee behind it

#53 specified `POST /books` as returning a path and an `already_exists` flag. Both halves are
wrong in ways that only surface later, so the write endpoints answer differently.

**A path is not identity.** `create_book_note` mints a Libris ID and returns only a `Path`, so
the endpoint as written would have handed the extension the one field that moves and withheld
the one that does not: `clean --rename` alone would move 132 files, and surviving exactly that
is what a Libris ID is for (ADR 0001). Every write therefore answers with the `libris_id`. The
path travels too, but as something to show a person - "added as Dune - Frank Herbert.md" -
never as a handle to the Book.

**A bare `already_exists` does not say what backs it.** ADR 0010 requires a response to state
which guarantee it gave. That looks redundant on a daemon which always checks the live Shelf,
until the extension is repointed at the remote by changing a base URL - which ADR 0010
promises is all it takes. The same client code then receives a duplicate check no fresher than
the last sync. If the answer does not carry its own guarantee, the extension has to infer
freshness from its own configuration, restoring exactly the coupling ADR 0010 removed. So a
write reports what happened - the Book Note was created, or the Book was already held - and
which guarantee stands behind it.

The vocabulary deliberately does not reuse ADR 0013's `absorbed`. That word describes an
Intent: a change recorded by a Surface and applied to the Shelf later by the CLI. The daemon
writes now, so no Intent exists and nothing is absorbed. Two mechanisms, two vocabularies,
so the sync protocol stays readable when both are in play.
