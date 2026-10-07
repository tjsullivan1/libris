# Hosted Libris

The hosted MCP server (#171, ADR 0035): a Container App that signs people in through Google and
lets one account in. It serves the same tools as `libris mcp`, answered from what `libris sync`
pushed to Cosmos (#175). The Library is read-only from here: `add_book` and `update_book` say
that changes can't be made remotely yet, and write nothing.

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
   identity signs in with GitHub's OIDC token, so it has no password. Then put the suffix into the
   `backend` block in `infra/versions.tf`, which can't read variables.

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

   None of these is an Azure credential. The variables are identifiers that grant nothing, and
   they stay variables so the logs stay readable: GitHub masks a secret's value wherever it
   appears, and the suffix is in every resource name. The Google account ID isn't a credential
   either, but the repository is public, and so are its logs, which print a variable's value.

## Deploying

A merge to `main` deploys, once CI has passed on it (`.github/workflows/deploy.yml`). The workflow
signs in to Azure as the deploy identity, applies `infra/`, builds the image tagged with the
commit SHA, moves the app to it, and waits for `/healthz` to answer. To deploy without a merge,
run the workflow from **Actions → Deploy → Run workflow**.

Terraform sets the app's image only when it creates the app. Every image after that is the
workflow's, so an apply by hand never rolls the app back to `image_tag`. An apply by hand still
works, with the same `terraform.tfvars`, and reads and writes the same state as the workflow.
Whichever runs second waits for the other's state lock.

## Moving an existing deployment onto the pipeline

A deployment from before #175 has local state and no deploy identity. Run step 1 with the
existing suffix (`terraform -chdir=infra output -raw suffix`), then:

```bash
cd infra
terraform init -migrate-state      # copies terraform.tfstate into the storage account
```

Add `suffix`, `owner_object_id` and `deploy_principal_id` to `terraform.tfvars`, and
`terraform apply`. The plan moves three resources to new names and destroys `random_string.suffix`,
which the pinned suffix replaces. Nothing in Azure changes except the two new role assignments for
the deploy identity. Once the migration works, delete the local `terraform.tfstate` and its backup:
they hold the signing key and the Google secret. Then do step 9.

## Pushing the Shelf

`libris sync` copies every Book Note into the `books` container, then rebuilds the word counts
that remote searches are weighed by (ADR 0033). It runs on the PC that holds the Shelf, signed in
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
the sync exits non-zero, while the rest still go up. Every sync pushes every note. Pushing only
what changed, and removing what left the Shelf, is #174.

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
- **Scale to zero.** The first call after an idle period waits for a cold start. #179 measures it.
- **The deploy identity is Owner of the app's resource group.** It applies `infra/`, which grants
  roles, and Contributor can't. It holds no role outside that group and the state container.
- **The state's storage account takes no access keys.** Everyone who reaches the state, CI or a
  person, does it through Entra. The state holds the signing key and the Google secret.
