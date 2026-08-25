# MCP and REST are two adapters over one service layer

The MCP tools are not the only client. `libris sync` drains pending Intents, pushes Book
Note state and acknowledges what applied — bulk work with no language model in the loop and
no ambiguity to hand back. Expressing that as agent tool calls would be a poor fit, so a
plain HTTP API is required from the start rather than being a later addition.

Both live in one ASGI app: `/mcp/*` and `/api/v1/*` are thin adapters over a single service
layer that owns resolution, creation, Intent recording and querying. Logic never lives in a
tool handler; if it did, the REST surface would have to reimplement matching.

The two adapters deliberately differ in shape. MCP tools are coarse and tolerant, taking
fuzzy input and handing ambiguity back as candidates for a person to settle. REST is fine
grained and exact, taking Libris IDs and returning 404 on a miss. A mobile app should speak
REST: MCP's tool discovery exists to help a language model choose, and an app already knows
what it wants.

Only the endpoints sync needs are built now. Query endpoints for a mobile app or a published
view are additive over the same service layer.

Extended by ADR 0010: there are three adapters, not two. The Edge extension (#51) reaches
the same service layer over a local daemon, and the count here was taken before that plan
was known.

The `/api/v1` prefix was revisited when #53 turned out to specify bare paths, and kept. A query
parameter cannot be routed on, so two versions would share a handler body and the version would
sit in the same namespace as the query data. A header keeps URLs clean but makes a hand-typed
curl diverge from what the extension receives, on a daemon meant to be debugged by hand.
Dropping versioning altogether was the tempting option, since ADR 0004 makes this single-tenant
and the many-clients-pinned-at-many-versions problem will never arrive here - but the client is
a browser extension that updates through a store on its own schedule, so a daemon briefly
serving an old extension and a new one is normal rather than exceptional. A router prefix costs
nothing to carry, and it is what makes ADR 0010's base-URL swap a configuration change rather
than a rewrite: the paths after the base URL have to be identical on both adapters.
