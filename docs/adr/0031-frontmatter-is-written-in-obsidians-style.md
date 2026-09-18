# Frontmatter is written in Obsidian's style

Follows ADR 0028 and ADR 0030.

Libris writes a Book Note's frontmatter the way Obsidian writes it. When Libris changes one
field, the other lines stay as they were.

Obsidian and PyYAML spell the same values differently. Before this, every Libris write (a
status, a rating, a merge, an enrichment) rewrote the whole block in PyYAML's spelling.
Measured by parsing every note on the Shelf and writing it back unchanged (#146), **2,969 of
3,083 notes changed on disk**:

| On disk (Obsidian) | Written back (PyYAML) |
|---|---|
| `rating:` | `rating: null` |
| `  - Frank Herbert` | `- Frank Herbert` |
| `title: "Boys' Club"` | `title: 'Boys'' Club'` |
| a long title on one line | folded at 80 columns |
| `isbn: "0786937521"` | `isbn: 0786937521` |

Changing one rating showed up as a 10-20 line diff in Obsidian Sync, file history or git,
which made the real change hard to find and made conflicts likelier when both tools touched a
note close together.

## One rule is about data, not style

PyYAML reads and writes YAML 1.1. Under 1.1, `0786937521` is not a number, because an octal
can't contain an 8, so PyYAML treats it as a string and writes it without quotes. Obsidian
reads YAML 1.2, where the same text is the integer 786937521 and the leading zero is gone. 35
ISBN-10s on the Shelf have that shape.

So a string that YAML 1.2 would read as a number is always double-quoted. Matching Obsidian's
style would not have fixed this on its own; it needs its own rule. The same rule covers other
things YAML 1.2 reads as numbers: `0o17`, `1e3`, `.inf`.

## How it is enforced

`dump_frontmatter_yaml` already had to be the one writer every rewrite goes through (ADR 0028),
so the style is set in one place. Its dumper:

- indents a list under its key
- writes nothing after an empty value's colon, not `null`
- uses double quotes wherever PyYAML would have used single ones
- never folds a line
- double-quotes any string YAML 1.2 would read as a number

`migrate` had its own quoting rule for the lines it writes by hand, and that rule missed the
same case: a title recovered as `1776` came out as `title: 1776`. It now goes through the
writer as well.

With this, 2,935 of 3,083 notes (95%) come back byte for byte. Every value reads back the
same, and writing a note a second time changes nothing.

## What this costs

The other 148 notes change once, the first time Libris writes them after this. 141 of them are
notes Libris itself wrote earlier in PyYAML's style. The other 7 hold a flow list such as
`tags: [Book]`, which becomes one item per line. PyYAML doesn't record which form a list was
written in, so keeping it would mean replacing the YAML library for 7 notes. The one-item-
per-line form is the one Obsidian uses for the other 2,936 lists on the Shelf. After the first
write, these notes stay put.
