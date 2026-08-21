# The note body leads with your notes, not the blurb

Measured across the Shelf: the median Book Note is 1,329 bytes and the median Google Books
description inside it is 1,031. The blurb is roughly three quarters of the average note, and
it is the one part fully re-derivable from an API call. Of 3,137 notes, 2,411 carry a
description, at least 15 of those are not in English, and only 248 contain a single sentence
the reader wrote. The `## Notes` heading meant to hold that writing is present in 1,386
notes and missing from 1,751.

So a machine-generated blurb, occasionally in the wrong language, was the dominant content
of every note, sitting above the 8% of notes holding something irreplaceable.

`## Notes` is normalised onto every Book Note and comes first. The description moves below it
into a collapsed `> [!abstract]-` callout: still indexed by Omnisearch, no longer dominating
the read view. Frontmatter is grouped rather than arbitrary — identity, bibliographic,
reading state, then dates — because nineteen fields in no order is unreadable in Obsidian's
Properties panel.

Dropping descriptions outright was tempting and would have shrunk the vault by about three
quarters. It was rejected only because it would end full-text search over blurbs in Obsidian.
They remain available remotely regardless (ADR 0006).

This changes the risk profile of the migration. It previously touched frontmatter alone; it
now rewrites bodies, including the 248 notes that hold real writing and one that runs to
26 KB. The dry run must be reviewed by eye before anything is written.
