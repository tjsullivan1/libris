# A write stamps the obvious date, and says that it did

Follows ADR 0016.

`update_book` sets only the fields it is given (the Intent rule), with one exception: setting
`status` to Read stamps `date_finished`, and to Reading stamps `date_started`, when the field
is empty. The response then names every field the Library set that the caller did not ask for,
so the stamp is reported rather than assumed.

Neither half works alone. Setting nothing means "I finished Oathbringer" records no date at
all, and the Shelf shows where that ends up: of 1,608 notes marked Read, only 704 carry a
`date_finished`, so the field is missing on the majority of the books it exists for. Stamping
silently is worse in the other direction, and this vault already holds the evidence - 82 notes
share `date_finished: 2020-12-11` and 28 share `2020-12-05`, which is one bulk operation
recording "now" across a batch and preserved since as fact. A wrong date is indistinguishable
from a right one afterwards, which is the silent-and-confident failure ADR 0003 refuses.

Disclosure is what separates them. A person who meant last Tuesday hears "marked Read, dated
today" and corrects it while still in the conversation - the same reason resolution happens
there (ADR 0003), applied to a field instead of to an identity.

Two rules keep it narrow. A stamp only fills an empty field, so re-marking a dated book never
overwrites the original; a re-read is not something the Library models and inventing it here
would be scope creep. And an explicit value always wins - the stamp is a fallback, never an
override.

Left unsolved: whose today. Over stdio the server runs on the reader's machine and the local
date is right. The same tool over Streamable HTTP (ADR 0007) reads a container's UTC clock,
which is another case of one definition giving two answers by location (ADR 0020).
