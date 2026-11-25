# The hosted server scales to zero, and the first call after a quiet spell waits for it

Settles the cold-start question in #179.

The hosted MCP server (ADR 0035) runs on a Container App with `min_replicas = 0`. After five
minutes without a request, the replica stops. The next request starts a new one and waits for it.
#179 asked for that wait to be measured and for a recorded choice: keep scale-to-zero, or keep one
replica running.

**The choice is to keep scale-to-zero.** A replica kept running would cost about as much as
everything else in the resource group combined, to save a wait that happens at most once per
conversation.

## What a cold start costs in time

Measured on 2026-11-25 against the deployed app (0.25 vCPU, 0.5 GiB, image 67 MB), with `curl`
sending the request Claude sends first: an unauthenticated `POST /mcp`, answered `401`.

| Started (UTC) | Replicas before | Time to first byte |
|---|---|---|
| 14:49 | 0 | 29.2 s |
| 15:10 | 0 | 29.7 s |
| 15:19 | 0 | 28.8 s |
| 15:27 | 0 | 29.7 s |

Four cold starts, 28.8–29.7 s, median 29.5 s. Before #179's tracing; see below for what it adds.

A warm replica answers the same request in 0.19–0.24 s, and the OAuth metadata in 0.20 s.

The platform's system log (`ContainerAppSystemLogs_CL`) breaks the first one down:

| Step | Took |
|---|---|
| Request arrives, scaler starts a replica, it's scheduled, sidecars start | 15 s |
| Image pull (cached on the node: 137 ms) and container start | <1 s |
| Python imports and app startup, until uvicorn reports it is ready | 9 s |
| Startup probe passes, ingress routes the waiting request | ~4 s |

The 15 seconds belong to the platform and nothing in this repository changes them. The 9 seconds
are mostly the `mcp` SDK: of the 2.5 s `libris.hosted` takes to import on a desktop, 2.0 s is
`mcp` itself. The tracing added in #179 adds another 0.9 s there, which is about 3.5 s on a
quarter of a vCPU.

Claude was not timed separately. The server cannot see how long a client waited, because its
first span starts once the app is up. `curl` from a desktop is the client-side measurement. Gemini
has not connected yet (#180).

## What a running replica costs in money

Container Apps consumption prices in eastus2, from the Azure retail price API on 2026-11-25. A
replica with no requests in flight is billed at the idle rate:

| | Rate | One idle replica for 30 days | After the monthly free grant |
|---|---|---|---|
| vCPU | $0.000003 per vCPU-s idle | 0.25 × 2,592,000 = 648,000 vCPU-s | 468,000 × rate = **$1.40** |
| Memory | $0.000003 per GiB-s idle | 0.5 × 2,592,000 = 1,296,000 GiB-s | 936,000 × rate = **$2.81** |

About **$4.21 a month**, or $5.83 if anything else in the subscription has used up the free grant
(180,000 vCPU-s and 360,000 GiB-s a month). The whole resource group cost $4.27 for November up
to the 25th, nearly all of it the registry's flat fee. So a warm replica would roughly double the
bill, and push it to about 95% of the $10 budget #179 set up, so the 80% alert would fire every
month.

## Why the wait is acceptable

- **It happens once per conversation, not once per call.** The replica stays up for five minutes
  after the last request. One conversation with a few tool calls pays the wait at most once.
- **Nothing is lost by it.** The transport is stateless and sign-ins live in Cosmos (ADR 0035), so
  a replica starting cold has nothing to recover.
- **The one known failure was a connector being added, not a tool call.** #180 records Claude's
  first connection attempt failing silently, probably because registration woke the app. Adding
  a connector is something done once, and doing it again while the app is warm works.

## When to revisit

- A client gives up on cold starts during ordinary tool calls, not just while connecting. App
  Insights shows this as a conversation's first request missing, or failing.
- The registry fee goes away. With the image on GitHub's free registry, the resource group would
  cost cents, and a warm replica would then be most of a smaller bill rather than a doubling.
  The money argument above would need redoing.
- Gemini (#180) turns out to time out sooner than Claude.

If a warm replica is ever wanted, it is one line: `min_replicas = 1` in `infra/main.tf`. Raising
`cpu` instead would shorten only the 9-second part, and is cheap under scale-to-zero because a
replica is billed at the active rate only while it works.
