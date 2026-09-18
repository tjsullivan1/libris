# A date field is text

Follows ADR 0005 and ADR 0028.

Every date in a Book Note's frontmatter - `date_added`, `date_published`, `date_started`,
`date_finished`, and any date Obsidian adds - is read as the text that spells it, and written
back unquoted when it is an ISO day.

PyYAML resolves an unquoted `2020-05-05` to a `datetime.date` and leaves a quoted
`'2020-05-05'` a string, so the type a field held depended on who wrote the note. Obsidian and
earlier imports wrote dates unquoted; Libris stamped them with `date.today().isoformat()`,
which PyYAML then quoted on write to keep it a string. Measured across 3,073 notes (#143):

```
date_added       3,055 date      13 str
date_published   2,199 date     770 str
date_started         6 date       0 str
date_finished      686 date       0 str
```

Neither type is wrong alone. Mixed, they fail quietly. `get_primary_book` compared two
`date_added` values with `<`, and a `date` against a `str` raises `TypeError`, which was caught
and fell through to "keep the first path" rather than the earlier note. A merge compared
`date_finished` values with `==`, and `date(2024, 3, 1) == "2024-03-01"` is False, so one day
spelled two ways stopped the merge as a conflict for the reader to settle. ADR 0003 is about
exactly this kind of confident wrong answer.

`date_published` decides which type. 686 of its values are a year alone and 70 a year and
month, as Google Books supplies them, and three are `199?`, `1916*` and `19??`. No `date` can
hold those, so "dates everywhere" was never available for that field, and one rule for four
fields beats two rules. Text also sorts correctly as ISO-8601, survives JSON without
conversion, and is what an export or an API has to emit anyway.

## How it is enforced

In one place, per ADR 0028. `parse_frontmatter_yaml` loads with a Safe loader whose timestamp
constructor returns the scalar's text; resolution is untouched, so the value is still
recognised as a timestamp and only what it is built into changes. `dump_frontmatter_yaml`, the
one writer every frontmatter rewrite goes through, drops the timestamp resolver so an ISO string
no longer looks like it needs quoting.

The writer half matters as much as the reader half. Reading dates as text with the stock
dumper would have quoted every date in every note Libris rewrote - 3,055 `date_added` lines
changing spelling for no reason. With both halves, an unquoted note round-trips byte for byte,
and the only lines that change on a rewrite are the ~30 quoted ISO dates Libris itself
stamped, which come out spelled like the rest. A year alone stays quoted, since unquoted YAML
would read it as a number.

## What this costs

Code that wants to do date arithmetic parses the text first. Nothing does today: the only
consumers that compared these values were the two merge sites above, and the export and MCP
server each carried their own date-to-string conversion, both now removed.
