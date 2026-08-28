# The status code is a coarse hint; the outcome lives in the body

Follows ADR 0016 and ADR 0020.

`GET /api/v1/books` answered a miss with 404 and a body carrying the Book Notes that might have
been the same Book. A 404 with a useful payload is a tell: the endpoint is not addressing a
resource, it is running a search, and a search that matches nothing has succeeded. It now
answers 200 with `{found, book, near_matches}`.

The failure that settles it belongs to this client. The extension points at a base URL a person
types (ADR 0019), so a mistyped port or path returns a 404 from something that is not Libris at
all, and the popup would render "not in your Library" for what is a misconfiguration. That is
precisely the silent wrong answer ADR 0003 refuses. The comment justifying the 404 cited ADR
0003, conflating two things: that ADR forbids fabricating a match or creating a Book Note as a
side effect of a miss, and `200 {"found": false}` honours it exactly. The status code was never
what made the answer honest.

Writes keep 201 for a Book Note created and 200 for one already held, and never 409. Already
held is not a failure - the state the caller wanted holds - and the outcome vocabulary is larger
than any set of status codes and grows by location: a write reports created or already held,
while the same call against the remote reports an Intent applied, absorbed or rejected (ADR
0013, ADR 0016, ADR 0020). Encoding the outcome in the status would make a client branch on
which adapter answered, which is the coupling ADR 0010 exists to remove. The status is a coarse
hint for caches and logs; the body is authoritative.
