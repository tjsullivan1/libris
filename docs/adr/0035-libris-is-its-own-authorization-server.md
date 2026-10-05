# Libris is its own authorization server, and Google says who you are

Supersedes the Entra half of ADR 0004. Narrows ADR 0007 and ADR 0020. Settles the approach for #171.

ADR 0004 had Entra sign in the person and ADR 0007 had the hosted MCP server sit behind it. Checking
what the two target clients require, before building anything, showed that would not work as written:

- **Claude** reaches an authorization server through Dynamic Client Registration, a Client ID
  Metadata Document, or a client ID typed in by hand. Entra supports neither of the first two. Claude
  also sends the MCP server URL as the token's resource, and Entra accepts a resource only as an
  Application ID URI on a domain the tenant has verified. That rules out the Container App's own
  `*.azurecontainerapps.io` hostname, so this path needs a custom domain before anything works.
- **Gemini's** consumer app (custom apps in Spark) expects the full chain: protected resource
  metadata, authorization server metadata, Dynamic Client Registration and PKCE. Without DCR it falls
  back to asking for a client ID and secret, and that fallback is where people report breakage.

Both clients work by default with DCR, and Entra does not offer it.

**So Libris is the authorization server.** The MCP SDK ships the endpoints: discovery, `/register`,
`/authorize`, `/token`, and the `401` that points a client at the metadata. Claude and Gemini talk
OAuth only to Libris, and Libris issues tokens whose audience is its own URL, so no custom domain is
needed. Libris implements the provider behind those endpoints and nothing more.

**Google says who is at the keyboard.** At `/authorize`, Libris sends the browser to Google, reads the
ID token Google returns, and issues a code only if the account's `sub` is the one allowed. ADR 0004's
single question, "is this Tim", now has a Google answer instead of an Entra one. The identity was a
Gmail account all along, and Entra federated to Google would have added a tenant and a hop without
adding anything. The check uses `sub`, not the email address, because Google can reassign an address
but never a `sub`. `email_verified` must also be true.

The ID token comes straight from Google's token endpoint over TLS, in exchange for Libris's client
secret, so its signature is not checked separately. OIDC Core 3.1.3.7 allows exactly that for a token
received this way. Its issuer, audience and expiry are still checked.

**A consent page stands between `/authorize` and Google.** Registration is open by design, and Google
signs in silently a person who is already signed in. Without an interruption, anyone could register a
client with their own redirect URI, send a link, and receive a code for this Library. The consent page
names the client and the host it will be sent back to, as the MCP authorization spec requires. The
page sets a `SameSite=Strict` cookie that its own form must return, so a page on another site cannot
submit the form for you.

**What is kept, and where.** Access tokens are signed JWTs that last an hour. Every call checks the
signature, issuer, audience and expiry, checks the subject against the allowed account, and checks
that the token's grant still exists.

**A grant is one sign-in.** Every token issued from a sign-in carries the grant's id, through any
number of refreshes. Revoking either the access token or the refresh token deletes the grant, which
ends both at once. Without it, revoking with an access token did nothing: the token lived out its
hour and its refresh token stayed valid, while `/revoke` reported success. The price is one Cosmos
point read per tool call, which is nothing at one person's volume.

The grant also catches a stolen refresh token. A rotated token stays behind as a spent marker for the
rest of its life. If it is presented again, one of its two holders is not the client it was issued
to, and there is no telling which, so the grant is deleted (RFC 9700 §4.14.2). Refusing only the
replay would have left a thief who redeemed the token first holding a live sign-in. The cost falls on
a client that retries a refresh whose answer it never received: it is signed out, not attacked.

**All of it lives in Cosmos** (ADR 0006 brings Cosmos to the project anyway): grants, registered
clients, refresh tokens, authorization codes and sign-ins in progress. A first draft kept codes and sign-ins in memory, and review caught why
that fails. The app scales to zero, and while a person is on Google's page no request reaches Libris,
so the sign-in it would come back to was gone. Holding no state in the process also means any
replica can finish a sign-in another one started. Codes and refresh tokens are kept as hashes, and
codes, refresh tokens and sign-ins are each taken once, by a delete conditioned on the version read.
Refresh tokens rotate on every use, as OAuth 2.1 requires for public clients.

**Open registration has limits.** Anyone can call `/register`, and each registration writes a
document. A client that goes 60 days without being issued a token is forgotten, and registrations
beyond 30 an hour are refused with a 429 before they reach Cosmos.

**Entra stays, for Azure only.** Terraform and `libris sync` reach Azure as the person's Azure account,
and the Container App reaches Cosmos and Key Vault as its managed identity (ADR 0006). None of those
are the sign-in a client presents.

## Consequences

- ADR 0020's three credentials become: nothing on stdio, a bearer token on the loopback daemon, and a
  Libris-issued OAuth token on the Container App. A Surface pointed at the remote, including the Edge
  extension one day, signs in through the same flow as Claude.
- Libris now holds security-sensitive code. It is kept small, it delegates everything the SDK already
  does, and its tests run the whole flow end to end against a fake Google.
- The allowed `sub` is configuration, not code. A sign-in by any other account is refused, and the
  refusal page shows the `sub` it saw, which is also how the first deployment finds the right value.
- Should multi-user access ever happen (ADR 0004), the allowlist and the consent page are where it
  starts, not the token format.

## What the spike showed (#171, 2026-10-02)

Claude connected and called a tool on the first deployment, on the Container App's own hostname. It
registered through DCR as a confidential `web` client with `claude.ai`'s callback, went through the
consent page and Google, exchanged its code, and called `ping`. No custom domain and no hand-entered
client were needed, which is the case this ADR was written for.

Claude's dialog offers three ways to identify itself. Only "Register automatically" works here:
Libris advertises DCR but not a Client ID Metadata Document.

The first attempt failed silently. Claude registered and then never sent the browser to `/authorize`.
That registration was the request that woke the app from zero, and Claude allows 10 seconds for it,
so a cold start is the likely cause. It is not proven, because the attempt that succeeded reached an
instance that was already running. #179 owns the measurement and the `min_replicas` decision.

Gemini was not tested. The design does not depend on it, but whether Gemini's custom apps accept this
server is still open.
