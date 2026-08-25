# An Intent that cannot apply is absorbed or rejected, not simply failed

An Intent arrives at the Shelf some time after the person who created it has gone, so what
happens when it does not fit matters more than it would in a request-response design. We had
deferred this; ADR 0010 made it urgent by pointing out that a duplicate caught at apply time
is the common case rather than an edge one.

Treating every Intent that does not apply verbatim as a failure would generate human work on
the most frequent path in the system. So an Intent has three terminal outcomes rather than
two:

- **Applied** - the Shelf changed as the Intent asked.
- **Absorbed** - the Library already satisfied the Intent. An add-intent for a Book already
  held is the ordinary case: the person wanted the Book in their Library and it is. The
  outcome carries the Libris ID the Intent turned out to mean, so the replica corrects
  itself rather than staying wrong.
- **Rejected** - the Intent could not apply and a person has to decide. This is narrow by
  design: an update-intent naming a Libris ID the Shelf no longer holds, or an Intent
  carrying a value the Library does not accept.

An add-intent that duplicates a held Book *and* carries fields the note contradicts is
absorbed, not rejected. An Intent names only the fields it changes, so applying those fields
to the note it turned out to mean is precisely what it asked for. We considered rejecting
this case to stop a stale Surface overwriting the Shelf, and rejected that: it reintroduces
conflict resolution, which ADR 0002 removes from the design deliberately.

Rejections reach the person through the Vault. `libris sync` runs unattended as a Scheduled
Task, so `typer.echo` reaches nobody, and the person is by construction somewhere else -
that is why the remote exists. Sync therefore rewrites a single Libris-owned note next to
the Shelf, in the folder the Shelf sits in rather than in the Shelf itself, listing what did
not apply and why. Obsidian Sync is on, so it is on the phone within minutes with no
infrastructure, and it is actionable where it lands: the fix for a dead Libris ID is to edit
the Book Note in Obsidian, which writes straight to the Shelf.

Two consequences worth stating. The note is a rendering, not a store - the remote stays
authoritative for Intent state, and the note is overwritten each run rather than
accumulating. And this is the first Markdown file Libris writes that is not a Book Note; it
must stay outside the Shelf, because `list_books` scans the Shelf directory and the Bases
view reads what it returns.

We rejected leaving rejections on the remote for a Surface to display when next opened. The
extension popup is a capture UI opened while buying a book, not an inbox, so a rejection
could sit unseen for months - the same silent wrongness ADR 0003 refuses. A push channel
reaches the person faster and remains open as an addition once workstream 4 exists.
