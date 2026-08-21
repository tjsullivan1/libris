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
