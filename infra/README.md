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

   Every resource name ends in a suffix, which keeps globally unique names unique (the
   registry, Key Vault, Cosmos, and the app's address). Terraform generates one unless you set
   `suffix` in `terraform.tfvars` (1-14 lowercase letters or digits). Changing it later renames
   and replaces every resource, including the app's address and therefore the Google redirect URI.

2. **Create the Google OAuth client.** In the Google Cloud console: create a project, then on the
   **OAuth consent screen** choose External and add yourself as a test user. Leaving the app in
   *Testing* is fine, because Libris asks Google only who you are and keeps no Google token. Then
   go to **Credentials → Create credentials → OAuth client ID → Web application**, and add the
   `google_redirect_uri` from step 1 as an authorized redirect URI.

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

6. **Find your Google account ID.** In Claude, go to **Settings → Connectors → Add custom
   connector** and enter `terraform -chdir=infra output -raw mcp_url`. Sign in when asked. The
   sign-in is refused, because no account is allowed yet, and the refusal page shows your Google
   ID. Add `allowed_google_sub = "<that id>"` to `terraform.tfvars` and apply again.

7. **Connect for real**:
   - **Claude**: remove the connector and add it again, sign in, and ask Claude to call the
     `ping` tool.
   - **Gemini**: at gemini.google.com go to **Settings → Connected apps → Add a custom app**,
     enter the same URL, sign in, and ask for `ping`. This needs a personal Google account in the
     US.

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

Without `LIBRIS_COSMOS_ENDPOINT`, clients and tokens are kept in memory and lost on restart.

## Things that are deliberate

- **One replica, at most.** Sign-ins in progress live in memory (ADR 0035).
- **Scale to zero.** The first call after an idle period waits for a cold start. #179 measures it.
- **Local Terraform state.** The state holds the signing key and the Google secret. #175 moves it
  to a storage account.
