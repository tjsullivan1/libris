# Hosted Libris

The hosted MCP server (#171, ADR 0035): a Container App that signs people in through Google and
lets one account in. It serves the same tools as `libris mcp`, answered from what `libris sync`
pushed to Cosmos (#175). `add_book` records an Intent in the `intents` container for the next
`libris sync` to apply (#176), dated the day it was asked (#214), and `update_book` records one
naming only the fields it changes (#177).

## What you need

- An Azure subscription, the Azure CLI and Terraform 1.9 or later:
  `winget install Microsoft.AzureCLI Hashicorp.Terraform`
- A Google account (the one the Library belongs to), and access to the Google Cloud console

Docker is optional, because the image builds in the registry.

## First deployment

The steps follow from four dependencies. Terraform's state needs somewhere to live before
anything else. The Google client needs the app's address. The app needs an image. And the right
Google account ID is only known once someone tries to sign in. After step 9, a merge to `main`
deploys on its own ([Deploying](#deploying)).

1. **Sign in to Azure and create the state storage and the deploy identity.** Pick a suffix
   first: 1-14 lowercase letters or digits. Every resource name ends in it, which keeps globally
   unique names unique (the registry, Key Vault, Cosmos, the state's storage account, and the
   app's address). Changing it later replaces every resource, including the app's address and
   therefore the Google redirect URI.

   ```bash
   az login
   export ARM_SUBSCRIPTION_ID=<id>
   terraform -chdir=infra/bootstrap init
   terraform -chdir=infra/bootstrap apply -var suffix=<suffix>
   ```

   `infra/bootstrap` keeps its own state locally (git-ignored). It holds no secret: the deploy
   identity signs in with GitHub's OIDC token, so it has no password. It also registers the Azure
   resource providers both roots use, since `infra/` runs with roles on one resource group and
   can't. Then put the suffix into the `backend` block in `infra/versions.tf`, which can't read
   variables.

2. **Create the registry and environment**, everything but the app:

   ```bash
   cd infra
   terraform init
   terraform apply -var deploy_app=false
   terraform output google_redirect_uri
   ```

   Before this, write `infra/terraform.tfvars` with what bootstrap printed. The file is
   git-ignored:

   ```hcl
   suffix              = "<suffix>"
   owner_object_id     = "<owner_object_id>"
   deploy_principal_id = "<deploy_principal_id>"
   budget_alert_email  = "<where budget alerts go>"
   ```

3. **Create the Google OAuth client.** In the Google Cloud console, create a project and open
   **Google Auth Platform**:
   - **Get started**: an app name and support email, audience **External**, and a contact email.
   - **Audience → Test users → Add users**: your Gmail address. Leave the app in *Testing*.
     Google expires a test user's consent after seven days, but that doesn't matter here: Libris
     asks Google only who you are, once per sign-in, and keeps no Google token.
   - **Clients → Create client**, type **Web application**. Under **Authorized redirect URIs**,
     add the `google_redirect_uri` from step 2. Leave **Authorized JavaScript origins** empty.

4. **Add the Google client to `infra/terraform.tfvars`**:

   ```hcl
   google_client_id     = "<client id>.apps.googleusercontent.com"
   google_client_secret = "<client secret>"
   # Optional: find_book works without it, at Google's lower anonymous quota.
   google_books_api_key = "<Google Books API key>"
   ```

5. **Build the image in the registry**, from the repository root:

   ```bash
   az acr build -r "$(terraform -chdir=infra output -raw registry)" -t libris-hosted:spike .
   ```

6. **Deploy everything**: `terraform -chdir=infra apply`. This also gives the deploy identity
   its roles on the app's resource group, which is what lets the workflow run.

7. **Add the connector in Claude and find your Google account ID.** Get the connector URL with
   `terraform -chdir=infra output -raw mcp_url`. Then, in claude.ai:
   - On Free, Pro or Max: **Customize → Connectors → Add custom connector**.
   - On Team or Enterprise, an Owner adds it under **Organization settings → Connectors → Add →
     Custom** (choose **Web** if asked), and you connect from **Customize → Connectors**.

   Paste the URL. If the dialog asks for settings, choose **Sign in now** and, for the OAuth
   client, **Register automatically**. Libris supports Dynamic Client Registration (ADR 0035) but
   doesn't publish the metadata that **Use Claude's published identity** needs. Leave the client ID
   and secret empty.

   Connect and sign in. The sign-in is refused, because no account is allowed yet, and the
   refusal page in the sign-in window shows your Google ID. Add `allowed_google_sub = "<that id>"`
   to `terraform.tfvars` and apply again.

8. **Connect for real**:
   - **Claude**: in **Customize → Connectors**, click **Connect** on the connector again and sign
     in. Changing the allowed account changes nothing in the connector's settings, so it doesn't
     need removing. Then [push the Shelf](#pushing-the-shelf) and ask Claude whether you've
     read a book you have.
   - **Gemini (untested).** Nobody has connected Gemini yet, and #180 tracks doing it. From
     Google's documentation, the route should be gemini.google.com, then **Settings → Connected
     apps → Add a custom app**, entering the same URL. It needs a personal Google account in the
     US. Treat the menu names as a guess, and correct them here once #180 is done.

9. **Let GitHub deploy.** In the repository's **Settings → Environments**, create `production`.
   Under **Deployment branches and tags**, choose **Selected branches and tags** and add `main`.
   The deploy identity accepts a token only from this environment, so this rule is what keeps a
   pull request or another branch from deploying. Then add to the environment:

   | Variable | Value |
   |---|---|
   | `AZURE_CLIENT_ID` | bootstrap's `azure_client_id` |
   | `AZURE_TENANT_ID` | bootstrap's `azure_tenant_id` |
   | `AZURE_SUBSCRIPTION_ID` | bootstrap's `azure_subscription_id` |
   | `LIBRIS_SUFFIX` | the suffix |
   | `LIBRIS_OWNER_OBJECT_ID` | bootstrap's `owner_object_id` |
   | `LIBRIS_DEPLOY_PRINCIPAL_ID` | bootstrap's `deploy_principal_id` |

   | Secret | Value |
   |---|---|
   | `GOOGLE_CLIENT_ID` | as in `terraform.tfvars` |
   | `GOOGLE_CLIENT_SECRET` | as in `terraform.tfvars` |
   | `GOOGLE_BOOKS_API_KEY` | as in `terraform.tfvars`, or leave it out to run keyless |
   | `LIBRIS_ALLOWED_GOOGLE_SUB` | the `allowed_google_sub` from step 7 |
   | `LIBRIS_BUDGET_ALERT_EMAIL` | the `budget_alert_email` from step 2 |

   None of these is an Azure credential. The variables are identifiers that grant nothing, and
   they stay variables so the logs stay readable: GitHub masks a secret's value wherever it
   appears, and the suffix is in every resource name. The Google account ID and the alert address
   aren't credentials either, but the repository is public, and so are its logs, which print a
   variable's value.

## Deploying

A merge to `main` deploys, once CI has passed on it (`.github/workflows/deploy.yml`). The workflow
signs in to Azure as the deploy identity, applies `infra/`, builds the image tagged with the
commit SHA, moves the app to it, and waits for `/healthz` to answer. To deploy without a merge,
run the workflow from **Actions → Deploy → Run workflow**.

Terraform sets the app's image only when it creates the app. Every image after that is the
workflow's, so an apply by hand never rolls the app back to `image_tag`. An apply by hand still
works, with the same `terraform.tfvars`, and reads and writes the same state as the workflow.
Whichever runs second waits for the other's state lock.

## Upgrading a deployment from before #179

#179 added Application Insights and a budget, which need two more resource providers, and a
required `budget_alert_email`. Before the first deploy that includes it:

1. Rerun bootstrap, which registers `Microsoft.Insights` and `Microsoft.Consumption`. The deploy
   identity can't, because registering is subscription-wide. A provider that's already
   registered is left as it is.

   ```bash
   terraform -chdir=infra/bootstrap apply -var suffix=<suffix>
   ```

2. Add `budget_alert_email` to `infra/terraform.tfvars`, and as the `LIBRIS_BUDGET_ALERT_EMAIL`
   secret. Without it, `terraform plan` stops and asks for it, and the workflow, which can't be
   asked, fails.

## Moving an existing deployment onto the pipeline

A deployment from before #175 has local state and no deploy identity. Run step 1 with the
existing suffix (`terraform -chdir=infra output -raw suffix`), then:

```bash
cd infra
terraform init -migrate-state      # copies terraform.tfstate into the storage account
```

Add `suffix`, `owner_object_id` and `deploy_principal_id` to `terraform.tfvars`, and
`terraform apply`. The plan moves four resources to new names and destroys `random_string.suffix`,
which the pinned suffix replaces. Nothing in Azure changes except the two new role assignments for
the deploy identity. Once the migration works, delete the local `terraform.tfstate` and its backup:
they hold the signing key and the Google secret. Then do step 9.

## Pushing the Shelf

`libris sync` copies the Book Notes that changed into the `books` container, deletes the documents
of notes that left the Shelf, then rebuilds the word counts that remote searches are weighed by
(ADR 0015, ADR 0033). It runs on the PC that holds the Shelf, signed in
as you through `az login`. Terraform gives your sign-in the data role for this, and no key is
involved.

```bash
uv sync --all-extras    # once: the sync extra brings the Azure SDKs
uv run --no-sync libris config --cosmos-endpoint "$(terraform -chdir=infra output -raw cosmos_endpoint)"
uv run --no-sync libris sync
```

In this repo, `--all-extras` rather than `--extra sync`. As with running the app locally, `uv sync`
removes any extra it isn't asked for, and the libris MCP server on the same PC needs `mcp`. Outside
the repo, install only what sync needs: `uv tool install 'libris[sync]'`.

It refuses to push anything while two notes share a Libris ID, and names them: the remote would
keep one and the Shelf two. A note it cannot push, such as one with no Libris ID, is named and
the sync exits non-zero, while the rest still go up. A new or emptied account, one with no word
counts, is sent every note whatever this PC's state file says. To run it unattended, see
`contrib/Register-LibrisSync.ps1` in the main README.

## Running it locally

```bash
uv sync --all-extras    # once: the hosted extra brings uvicorn and the Azure SDKs
LIBRIS_PUBLIC_URL=http://localhost:8000 LIBRIS_GOOGLE_CLIENT_ID=x \
LIBRIS_GOOGLE_CLIENT_SECRET=x LIBRIS_TOKEN_SIGNING_KEY=$(python -c "print('k'*48)") \
uv run --no-sync uvicorn --factory libris.hosted:create_app_from_env
```

`--all-extras` rather than `--extra hosted`, because `uv sync` removes any extra it isn't asked
for, and the `server` extra would go. `--no-sync` on the run is the AGENTS.md habit: without it, uv
reinstalls the project, which fails while a libris MCP server holds `libris.exe` open. That also
applies to the `uv sync`. If it fails that way, stop the MCP server and run it again.

Without `LIBRIS_COSMOS_ENDPOINT`, clients, tokens and sign-ins are kept in memory and lost on
restart, and there is no Library: the tools answer that the endpoint is not set.

## Finding a request

Every request and every tool call is traced to Application Insights
(`terraform output -raw application_insights`). Each tool call is a span named for the tool,
such as `tools/call find_book`, inside the `POST /mcp` request it arrived on. A tool that
answered with an error carries `error.type = tool_error`. The liveness probe is not traced.

In the portal, open the Application Insights resource, then **Transaction search**, and search
for the tool's name. Or query the workspace from **Logs**:

```kusto
AppRequests
| where TimeGenerated > ago(1d) and Name startswith "tools/call"
| project TimeGenerated, Name, Success, DurationMs, OperationId
| order by TimeGenerated desc
```

The SDK's tool span is a server span, so it is a row in `AppRequests` like the `POST /mcp` it
sits in. The two share an `OperationId`, which also finds any Cosmos call the tool made, in
`AppDependencies`, along with its Google Books calls.

No query string is recorded. The sign-in carries credentials there: Google's authorization code,
the PKCE challenge, the pending consent. A request span keeps only its path, and the Google Books
API key travels in a header rather than the URL, so it doesn't appear in spans, errors or logs.
uvicorn's access log is off for the same reason.

Telemetry is sent as the app's managed identity: the resource accepts nothing else. The app's
role on it is created in the same apply that switches tracing on, and a new role takes a minute or
two to start working, so the first minutes of telemetry after that first apply can be refused.

Container stdout (startup, shutdown, and the MCP SDK's own log lines) still goes to the Log
Analytics workspace as `ContainerAppConsoleLogs_CL`, and scaling and probe events as
`ContainerAppSystemLogs_CL`.

The workspace takes at most 0.2 GB a day. One person's traffic is megabytes. If a day's
ingestion reaches the cap, the rest of that day's telemetry is dropped. That's a sign of a loop,
not of normal use.

## Cost

A budget on the resource group (`monthly_budget`, $10 by default) emails `budget_alert_email` at
80% and 100% of actual spend, and when the month is forecast to pass 100%. Set the address in
`terraform.tfvars` and as the `LIBRIS_BUDGET_ALERT_EMAIL` repository secret. It is a personal
address and the workflow logs are public, so it is never a variable.

Almost all the spend is the container registry's flat Basic fee, about $5 a month. Everything
else together came to under $0.05 in October 2026, and the Container App stayed inside the
monthly free grant. An alert therefore means something changed.

## If Cosmos loses data

Almost nothing in Cosmos needs a backup, because almost all of it lives somewhere else:

| Container | What's in it | Getting it back |
|---|---|---|
| `books`, `word_counts` | What `libris sync` pushed from the Shelf | Delete `sync-state.json` from Libris's config directory and run `libris sync`. Every note goes up, and sync rebuilds its record of what Cosmos holds. |
| `auth` | Registered clients, refresh tokens | Nothing to recover. Each connector signs in again. |
| `intents` | Adds and updates from Claude that sync hasn't applied yet | Nowhere else. Usually there are none: the scheduled sync applies them. |

So the only loss is the Intents made since the last sync. Asking Claude again is usually
quicker than any restore.

The account keeps Azure's default periodic backup: one every 4 hours, kept for 8 hours, in
geo-redundant storage. Restoring from it means opening a support request in the portal
(**Help + support**, Cosmos DB, restore). It has to be opened within those 8 hours, and Microsoft
restores into a new account. Copy the `intents` documents you need from that account into the live
one, then delete it.

Continuous backup, which restores to any second without a ticket, was considered in #179 and left
out. Switching to it is one-way, it isn't documented as working on serverless accounts, and it
would protect a few hours of Intents at most.

## Troubleshooting

**`RequestDisallowedByAzure ... without authenticating through MFA`**. Azure has required MFA for
every create, update or delete made from the CLI or an IaC tool since October 2025. Read-only calls
don't need it, so `init` and `plan` work and `apply` fails. Run `az logout` and then
`az login --tenant <tenant id>`, and complete the MFA prompt. If the error persists, your tenant
doesn't ask for MFA at sign-in. Attempt any small write with the CLI (for example `az group create`),
and it refuses with the exact `az login ... --claims-challenge "..."` command to run instead. Don't
repeat the write afterwards; let Terraform create the resource. See
[Microsoft's guide](https://learn.microsoft.com/en-us/cli/azure/use-azure-cli-successfully-troubleshooting#troubleshooting-multifactor-authentication-mfa).

## Things that are deliberate

- **One replica.** Enough for one person. Sign-ins are kept in Cosmos, not in memory, so this is a
  cost choice rather than a correctness one (ADR 0035).
- **Scale to zero.** The first call after five idle minutes waits for a cold start of about 30
  seconds. ADR 0037 has the measurements and why that wait is kept.
- **The deploy identity is Owner of the app's resource group.** It applies `infra/`, which grants
  roles, and Contributor can't. It holds no role outside that group and the state container.
- **The state's storage account takes no access keys.** Everyone who reaches the state, CI or a
  person, does it through Entra. The state holds the signing key and the Google secret.
