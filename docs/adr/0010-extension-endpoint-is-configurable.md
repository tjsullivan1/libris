# The extension's endpoint is configurable, and duplicate checks declare their authority

Extends ADR 0008.

The Edge extension (#51) could talk to a localhost daemon or to the remote service. Routing
it only through Azure would gate a capture surface that needs no infrastructure on four
workstreams that do, and would break capture whenever the machine is offline — for a task
performed in a browser three feet from the Shelf. Routing it only through localhost would
give up adding books from a work laptop or a phone browser, which is the stated reason the
remote exists at all.

So the service layer is built once and both adapters sit over it: `libris serve` on
loopback now, the Container App later. The extension stores a base URL and a credential, so
moving it to the remote is configuration rather than a rewrite.

The two adapters are not equivalent, and the difference is behavioural rather than
transport. A local daemon checks for duplicates against the live Shelf and can promise "this
Book was not in your Library, and now it is." The remote checks against a replica no fresher
than the last sync, and can only promise "not as far as I knew, and it will be soon."
Responses therefore state which guarantee was given, so a Surface can say "added" or
"queued" truthfully rather than guessing.

This makes the deferred question of what happens to an Intent that cannot apply urgent
rather than theoretical: a duplicate caught at apply time is now the common case, and it
needs a path back to the person who clicked the button.

Narrowed by ADR 0020: "a base URL and a credential" was two fields where three are needed. The
paths and the browser permission model survive a move to the remote; the credential does not,
because the daemon takes a bearer token and the Container App takes Entra (ADR 0004). A client
stores the base URL, the auth mode, and the credential.
