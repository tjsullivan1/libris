# Libris owns `title`; the linter's `yaml-title` rule is disabled

Obsidian Linter's `yaml-title` rule was configured with `mode: filename`. Book Notes are
named `Title - Author.md`, so every time the linter touched a note it wrote `Title - Author`
into the `title` field — putting the author inside the title.

That is not historical damage like the corrupted headings and aliases. It recurs on every
edit, so repairing titles without changing the rule would achieve nothing. It also made a
newly working feature dangerous: `clean --rename` had been a no-op for the life of this
vault because it read a key no note carried, and once that was fixed it would have renamed
156 files, some into `Title - Author - Author.md`.

The rule is therefore disabled vault-wide and Libris owns `title`, which it already sets
from Google Books. Nothing is deleted; existing titles remain and simply stop being
rewritten. The cost is that renaming a non-book note no longer updates its title
automatically. That cost is small: of 1,350 non-book notes, 809 carry no `title` at all and
work fine, and of the 383 that do, 272 merely repeat their filename. `Reading List.base` is
the only Bases view in the vault and it queries Book Notes.

`yaml-title-alias` stays enabled. It is the rule doing useful work, and ADR 0009 depends on
it.

Scoping the change to the Book List folder via `foldersToIgnore` was rejected: it is
all-or-nothing per folder and would have disabled `yaml-title-alias` there too.
