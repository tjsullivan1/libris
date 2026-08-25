# The note body leads with your notes, not the blurb

Measured across the Shelf: the median Book Note is 1,329 bytes and the median Google Books
description inside it is 1,031. The blurb is roughly three quarters of the average note, and
it is the one part fully re-derivable from an API call. Of 3,137 notes, 2,411 carry a
description, at least 15 of those are not in English, and only 248 contain a single sentence
the reader wrote. The `## Notes` heading meant to hold that writing is present in 1,386
notes and missing from 1,751.

So a machine-generated blurb, occasionally in the wrong language, was the dominant content
of every note, sitting above the 8% of notes holding something irreplaceable.

A Book Note opens with an `# Title` H1, then `## Notes`, then the description in a collapsed
`> [!abstract]-` callout: still indexed by Omnisearch, no longer dominating the read view.
`## Notes` is normalised onto every note; it is currently present on only 1,386 of 3,137.

The H1 is load-bearing and must not be tidied away. Obsidian Linter's `yaml-title-alias`
rule is enabled with `keep-alias-that-matches-the-filename: false`, so it derives an alias
from the first heading. With no H1 it took `## Notes` or `### Description`, which is how
eleven notes came to be aliased "Notes" and three "Description" — those notes carry a
corrupted `# Notes` or `# Description` heading too, and the migration repairs both. With a
correct H1 it mints the title as an alias, which is what lets `[[Dune]]` resolve against a
file named `Dune - Frank Herbert.md`.

Aliases are left to the linter rather than written by the migration. It owns the rule and
its own formatting conventions, and hand-writing them invites a fight the next time it runs.
The consequence is accepted: aliases exist on 240 notes today and will spread only as notes
are edited. Frontmatter is grouped rather than arbitrary — identity, bibliographic,
reading state, then dates — because nineteen fields in no order is unreadable in Obsidian's
Properties panel.

Dropping descriptions outright was tempting and would have shrunk the vault by about three
quarters. It was rejected only because it would end full-text search over blurbs in Obsidian.
They remain available remotely regardless (ADR 0006).

This changes the risk profile of the migration. It previously touched frontmatter alone; it
now rewrites bodies, including the 248 notes that hold real writing and one that runs to
26 KB. The dry run must be reviewed by eye before anything is written.
