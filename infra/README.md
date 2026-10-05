# Hosted Libris

The hosted MCP server (#171, ADR 0035): a Container App that signs people in through Google and
lets one account in. This is the auth spike. Its only tool is `ping`, and the Library's tools
arrive with #175.

## What you need

- An Azure subscription, the Azure CLI and Terraform 1.9 or later:
  `winget install Microsoft.AzureCLI Hashicorp.Terraform`
- A Google account (the one the Library belongs to), and access to the Google Cloud console

Docker is optional, because the image builds in the registry.

## First deployment

The steps follow from three dependencies. The Google client needs the app's address. The app
needs an image. And the right Google account ID is only known once someone tries to sign in.

1. **Sign in to Azure and create the registry and environment**

   ```bash
   az login
   export ARM_SUBSCRIPTION_ID=<id>     # or set subscription_id in terraform.tfvars
   cd infra
   terraform init
   terraform apply \
     -target=azurerm_container_registry.main -target=azurerm_container_app_environment.main
   terraform output google_redirect_uri
   ```

   In PowerShell, quote each `-target`. Otherwise PowerShell splits the argument at the `.` and
   Terraform reports "Too many command line arguments":

   ```powershell
   az login
   $env:ARM_SUBSCRIPTION_ID = "<id>"
   cd infra
   terraform init
   terraform apply '-target=azurerm_container_registry.main' '-target=azurerm_container_app_environment.main'
   terraform output google_redirect_uri
   ```

   Every resource name ends in a suffix, which keeps globally unique names unique (the
   registry, Key Vault, Cosmos, and the app's address). Terraform generates one unless you set
   `suffix` in `terraform.tfvars` (1-14 lowercase letters or digits). Changing it later renames
   and replaces every resource, including the app's address and therefore the Google redirect URI.

2. **Create the Google OAuth client.** In the Google Cloud console, create a project and open
   **Google Auth Platform**:
   - **Get started**: an app name and support email, audience **External**, and a contact email.
   - **Audience → Test users → Add users**: your Gmail address. Leave the app in *Testing*.
     Google expires a test user's consent after seven days, but that doesn't matter here: Libris
     asks Google only who you are, once per sign-in, and keeps no Google token.
   - **Clients → Create client**, type **Web application**. Under **Authorized redirect URIs**,
     add the `google_redirect_uri` from step 1. Leave **Authorized JavaScript origins** empty.

3. **Write `infra/terraform.tfvars`**. The file is git-ignored.

   ```hcl
   google_client_id     = "<client id>.apps.googleusercontent.com"
   google_client_secret = "<client secret>"
   ```

4. **Build the image in the registry**, from the repository root:

   ```bash
   az acr build -r "$(terraform -chdir=infra output -raw registry)" -t libris-hosted:spike .
   ```

5. **Deploy everything**: `terraform -chdir=infra apply`

6. **Add the connector in Claude and find your Google account ID.** Get the connector URL with
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

7. **Connect for real**:
   - **Claude**: in **Customize → Connectors**, click **Connect** on the connector again and sign
     in. Changing the allowed account changes nothing in the connector's settings, so it doesn't
     need removing. Then ask Claude to call the `ping` tool.
   - **Gemini (untested).** Nobody has connected Gemini yet, and #180 tracks doing it. From
     Google's documentation, the route should be gemini.google.com, then **Settings → Connected
     apps → Add a custom app**, entering the same URL. It needs a personal Google account in the
     US. Treat the menu names as a guess, and correct them here once #180 is done.

## Updating the app

```bash
az acr build -r "$(terraform -chdir=infra output -raw registry)" -t libris-hosted:<tag> .
terraform -chdir=infra apply -var image_tag=<tag>
```

A new tag every time. Reusing a tag leaves the running revision on the old image.

## Running it locally

```bash
LIBRIS_PUBLIC_URL=http://localhost:8000 LIBRIS_GOOGLE_CLIENT_ID=x \
LIBRIS_GOOGLE_CLIENT_SECRET=x LIBRIS_TOKEN_SIGNING_KEY=$(python -c "print('k'*48)") \
uv run --no-sync --extra hosted uvicorn --factory libris.hosted:create_app_from_env
```

Without `LIBRIS_COSMOS_ENDPOINT`, clients, tokens and sign-ins are kept in memory and lost on
restart.

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
- **Local Terraform state.** The state holds the signing key and the Google secret. #175 moves it
  to a storage account.
