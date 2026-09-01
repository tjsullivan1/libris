# Location changes the credential and the Duplicate Guarantee, never the shape

Narrows ADR 0007 and ADR 0010.

Libris will run one service layer under two adapter shapes - MCP and REST (ADR 0008) - in two
locations: this PC and the Container App. That is four surfaces, and the ADRs describing them
were each written about one of them, so each stated the local/remote difference as though it
were its own problem.

It is one problem. Changing adapter shape changes nothing that matters: MCP is coarse and
tolerant, REST is fine grained and exact, and both call the same service layer over the same
paths. Changing location changes exactly two things, and the same two in either shape.

**The credential.** Nothing on stdio, a bearer token on the loopback daemon, Entra on the
Container App (ADR 0004, ADR 0007). No client can present two of these, so a client stores a
base URL, the auth mode that endpoint uses, and the credential for it - three fields, not the
two ADR 0010 assumed. The Edge extension is the first client to need this and implements
`bearer` alone.

**The Duplicate Guarantee.** A local surface checks for duplicates against the live Shelf and
writes a Book Note now. A remote surface checks a replica no fresher than the last sync and
records an Intent for the CLI to apply later (ADR 0002). Every write therefore states which
guarantee backs it, so a Surface can say "added" or "queued" truthfully rather than inferring
it from its own configuration. ADR 0016 put this in the REST write response; it belongs in
every response that reports a write, MCP tool results included. The outcome vocabularies differ
alongside it, deliberately: a write reports created or already held, an Intent reports applied,
absorbed or rejected (ADR 0013, ADR 0016).

This narrows two sentences written before the pattern was visible. ADR 0010's "moving it to the
remote is configuration rather than a rewrite" holds for the paths and, with optional host
permissions, for the browser's permission model (ADR 0019) - it does not hold for the
credential. ADR 0007's "the tool definitions do not change between them" holds for the
signatures and not for what they answer.
