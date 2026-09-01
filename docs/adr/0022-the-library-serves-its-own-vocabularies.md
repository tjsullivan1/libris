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
