# Libris keeps its own authorization server, and adds CIMD only when a client needs it

Amends ADR 0035. Settles #182.

ADR 0035 made Libris its own authorization server, and #181 built it as `oauth.py`: about 950 lines
of security code that only this repo maintains. #182 asked whether FastMCP's `OAuthProxy`, with its
`GoogleProvider`, should replace it. The case for switching was never that FastMCP is safer. It was
that someone else would maintain it, and that it already supports a Client ID Metadata Document
(CIMD), the default way to identify a client in the MCP spec's 2026-07-28 revision.

**The answer is to keep `oauth.py`.** Reading FastMCP 4.0.11's source on 2026-10-07 settled the
questions #182 left open:

- **The cheap moment to switch has passed, and switching is now a rewrite.** #182 asked for the
  decision before #175 wired the real tools into `hosted.py`. #175 shipped first, on the official
  `mcp` SDK. FastMCP 4 no longer builds on that SDK: it depends on `mcp-types` alone. Moving would
  rewrite the MCP layer: the tools, the stdio server for Claude Code, `hosted.py`, and their tests.
  ADR 0020's promise of the same tools everywhere would have to be re-proven on the new framework.
- **Its consent cookies are as sound as ours.** They carry the `__Host-` prefix over HTTPS and are
  signed, the consent form carries a CSRF token, and the weaker unprefixed name is refused over
  HTTPS. A sibling Container App cannot plant an approval. #182's comparison had this as unknown.
- **Its refresh tokens are weaker than ours.** Rotation reads a token and deletes it in two separate
  steps, so two refreshes racing with one token can both succeed. A replayed token is only "not
  found", and the grant it came from stays alive, so a thief who refreshed first keeps a working
  sign-in. Ours marks a rotated token spent with a conditional write and deletes the whole grant on
  a replay (ADR 0035, RFC 9700 §4.14.2).
- **Its revocation is per token.** Revoking deletes that refresh token and asks Google to revoke
  Google's own token. Nothing ends every token from one sign-in at once, which is what ADR 0035's
  grant exists to do.
- **It holds more and calls out more.** A proxy keeps the person's Google access and refresh tokens,
  encrypted, and checks every tool call against Google's `tokeninfo` and `userinfo` endpoints: two
  outbound requests per call. Libris keeps no Google token at all, because Google only says who
  signed in. A tool call costs a local signature check and one Cosmos point read. On an app that
  scales to zero, fewer outbound calls is fewer things to wait on.
- **The one-account rule would still be ours to write.** `GoogleProvider` exposes the `sub` but does
  not restrict it. That check is a small part of `oauth.py`, not the bulk of it.

What FastMCP offers that Libris lacks is CIMD and outside maintenance. Neither outweighs a rewrite
that weakens refresh tokens and revocation.

**CIMD waits until a client needs it.** Claude falls back to Dynamic Client Registration when CIMD
isn't advertised, and #171's spike connected Claude that way. DCR is deprecated in the 2026-07-28
revision but still supported. CIMD is also not a small addition. A `client_id` that is a URL has
Libris fetch whatever document the caller names. Doing that safely means refusing internal
addresses (including Azure's metadata endpoint and DNS that resolves to one), and bounding
redirects, size and time. It also means caching the document and checking its redirect URIs.
FastMCP spends about 1,400 lines on CIMD and its fetch guards. Building that early would add a new
kind of exposure to code that ten review rounds have just hardened, for a client that doesn't ask
for it yet.

Revisit this decision when any of these happens:

- a client Libris targets requires CIMD and no longer falls back to DCR;
- an MCP spec revision removes DCR;
- #180 finds that Gemini will not connect through DCR.

**A custom domain is a separate decision.** It would remove the shared `azurecontainerapps.io`
parent that ADR 0035 defends against with `__Host-` cookies and an exact `Origin` check. Those
defences already hold, so a domain is one more layer. Whether it is worth having is a cost and
operations question, and it would be the same question under either implementation.

## Consequences

- `oauth.py` stays, and so does its maintenance. A security advisory against the official `mcp`
  SDK's auth handlers, or against FastMCP's equivalents, is worth reading for flaws Libris might
  share. #181's review found the same callback-binding flaw that FastMCP fixed as CVE-2026-27124.
- Libris still advertises DCR only, so a client must register automatically. ADR 0035's note that
  only Claude's "Register automatically" option works still stands.
- #180, Gemini signing in, is now also the earliest check on one of the three triggers above.
