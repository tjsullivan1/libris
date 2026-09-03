# The Library serves its own field vocabularies

Follows ADR 0005 and ADR 0020.

A Surface that offers a person a status or a format has to know which values the Library
defines. Those live in `note_format.py`, and every client that restates them becomes a copy that
can drift. One already has: `libris update` offers "Finished", which is not one of the four
values a status may hold, and omits "Not To Read" - so an interactive prompt offers a value the
write path rejects.

So `GET /api/v1/fields` answers with the vocabularies, and clients render from what it says
rather than from a list of their own. It sits under `/api/v1` and behind the token because a
field vocabulary is Library data; `/health` stays unversioned and open because it reports on the
daemon process instead (ADR 0008).

The reason this is worth a request for three constant lists is ADR 0020. The extension is built
to be repointed at the remote by changing a base URL, and ADR 0005 makes the vault's own
vocabulary canonical - so "the values a status may hold" is a property of the Library being
talked to, not of the client talking. A hardcoded list bakes one Library's answer into a client
designed for two.

`rating` is left unconstrained, as it is today. Nothing in `FIELD_VOCABULARIES` bounds it, and
inventing a rule here would put the vocabulary's fourth definition in the place this decision
exists to prevent.

Extended for MCP: the tools carry the vocabularies as JSON Schema enums on `update_book` and
`add_book`, generated from `FIELD_VOCABULARIES` at server start, rather than exposing a `fields`
tool. This is the same decision, not an exception to it - the objection is to a client restating
the vocabulary, and generating a schema from the one definition is the Library serving its own.
A model reads a tool schema before it composes a call, whereas a `fields` tool is one it can
simply not call, which returns the guess this ADR exists to prevent. The cost is that the schema
is built once per process, so a vocabulary change needs a restart.

Correction of fact, not of decision: the example above is stale. The prompt that offered
"Finished" belongs to `libris status`, not a `libris update` that has never existed, and it was
changed to render from `STATUS_VALUES` in 44713a9 - the same commit that added the endpoint. The
drift was real when this was written and is what the decision rests on; nothing in `src/` offers
the value now.
