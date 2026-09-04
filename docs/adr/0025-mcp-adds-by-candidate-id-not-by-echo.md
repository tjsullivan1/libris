# MCP names a Book Candidate by its id; only REST echoes one back

Follows ADR 0008 and ADR 0012.

`POST /api/v1/books` takes a whole Book Candidate back - title, authors, isbn, page count,
genres, thumbnail and the full description. The MCP `add_book` takes a Google Books id and
re-fetches the volume itself.

The difference is the client, not the transport. The extension holds the candidate object it
was given and returns it unchanged, so echoing costs nothing and proves nothing. A language
model re-emits the record token by token, which pays for the description twice and, more
seriously, gives the model an opportunity to alter what it is writing - tidying a title,
dropping a subtitle, paraphrasing a blurb. The resulting Book Note then asserts something the
source never said, with nothing downstream able to tell. ADR 0012 makes Libris the owner of
the title field; echoing hands it to whatever the model felt like emitting.

Naming the candidate by id means only its identity crosses the boundary and the metadata is
always fetched by the code that fetched it the first time. The cost is a `GET /volumes/{id}`
that `GoogleBooksClient` does not have yet, and one extra round trip to Google per add.

Server-side handles were rejected: caching candidates and returning opaque tokens puts session
state in a stdio process that dies between conversations, and an expired handle mid-flow has no
good failure message.

ADR 0008 predicted the two adapters would diverge, with MCP the coarse and tolerant one. Here it
is the stricter of the two, and for the same underlying reason: the shape follows what the
caller can be trusted to hold onto.

A book Google Books does not have cannot be added over MCP at all under this decision. Left out
of the first cut rather than built as a second, unvalidated write shape.
