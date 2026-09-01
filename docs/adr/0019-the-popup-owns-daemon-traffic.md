# The popup owns daemon traffic, over a host permission

Extends ADR 0010.

#54 put the daemon traffic in a background service worker, and #52 built a CORS allowlist
(`extension_origins`) to admit the extension's origin. Measured in Edge, both were answering a
problem that does not exist. A `fetch` to a URL covered by `host_permissions` is granted that
origin outright and no preflight is attempted: two unpacked extensions differing only in that
one manifest line were loaded against a server sending no CORS headers, and the permitted one
passed every case from both contexts with zero `OPTIONS` requests reaching the server, while
the unpermitted one failed every one.

So the popup owns every call to the daemon. The content script only scrapes the page and hands
the payload back, because a content script is bound by the host page's CORS and cannot reach
loopback. The service worker carries no traffic: it is indistinguishable from the popup in what
it is permitted to do, and it is torn down after roughly thirty seconds idle - which a lookup
waiting on a person choosing between candidates will routinely exceed.

`extension_origins` stays, and stays empty. It never guarded the daemon: in the spike an
unpermitted simple GET still reached the server and only the response was withheld, so the
bearer token is the guard and always was. What the allowlist decides is whether an ordinary web
page in any open tab can read a reply, and that answer should stay no.

The manifest fixes only the default daemon, `http://127.0.0.1:8787/*`, so a first run needs no
grant. Anything else the options page can be pointed at - any loopback port, and any host over
TLS - is declared optional and requested one origin at a time from the options page on a user
gesture. Without that, a server URL a person can type is decorative: the field would accept a
port the manifest does not cover, and the extension would quietly stop reaching anything.

Nothing in the extension assumes Obsidian, or the Shelf itself, is present on the device running
it. A Book Note is shown by title and authors; its path travels for display only and is rendered
as text, never as an `obsidian://` link. The extension is meant to run in a browser on a machine
holding no vault - that is the case the remote adapter exists to serve (ADR 0010) - and a deep
link that works on the desktop and silently does nothing on the phone is worse than no link.

The extension holds no site permissions either. #54 asked for a generic scraper so unlisted
sites degrade gracefully and, in the same paragraph, for host permissions scoped to the two
supported sites - which cannot both hold, because a declared content script only runs where
there is already host permission. So nothing is declared: `activeTab` and `scripting` let the
popup inject a scraper into the current tab when a person clicks the toolbar icon, chosen by
hostname with the generic one as the fallback. The permission surface ends up smaller than the
issue asked for, and the generic scraper works everywhere rather than only on the two sites that
do not need it. The price is that the extension cannot see a page until it is clicked, so it can
never badge its icon to say "this is a book page" - that affordance would cost back exactly the
site permissions this avoids.
