# An MCP add stops on a Near Match; the REST one reports and writes

Follows ADR 0003 and ADR 0010.

`service.add_book` refuses to write only on an exact match - a shared ISBN, a shared Google
Books id, or a normalised title and first author. Near Matches are the fuzzy remainder, and
`GET /api/v1/books` hands them back beside a miss for a person to settle. The MCP `add_book`
goes further: when Near Matches exist it writes nothing, returns them, and requires an explicit
confirmation to proceed.

The difference is where the person is standing. The extension shows Near Matches in a popup
next to the button, so a human sees them before clicking. Over MCP the caller is the model, and
a near match reported alongside a completed write arrives after the file exists - which turns
one question into a cleanup task. ADR 0003 puts ambiguity back to the Surface while the person
is still present to answer it; here the Surface only creates that moment if the tool refuses
first.

The cost is real and known. `find_similar` conflates 83 pairs on this Shelf, some genuine
variants and some different books, so the confirmation will be asked for when it need not be.
That trade is accepted because the two errors are not symmetric: a needless question costs one
exchange, while a duplicate Book Note is permanent, splits a reader's writing across two files,
and is found only by a duplicate sweep months later. The Shelf already carries three such
groups.

Every write also reports its Duplicate Guarantee (ADR 0016), constant over stdio and not
constant once Streamable HTTP lands with the same tool definitions (ADR 0007, ADR 0020).
