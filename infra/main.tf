# The hosted MCP server (#171): one Container App behind Libris's own OAuth
# (ADR 0035), its secrets in Key Vault, and clients and refresh tokens in
# Cosmos serverless. Everything the app reaches, it reaches as its managed
# identity, so no connection string or registry password exists (ADR 0006).

data "azurerm_client_config" "current" {}

# Generated only when no suffix is given.
resource "random_string" "suffix" {
  count   = var.suffix == null ? 1 : 0
  length  = 6
  upper   = false
  special = false
}

locals {
  suffix   = var.suffix != null ? var.suffix : random_string.suffix[0].result
  app_name = "libris-mcp-${local.suffix}"
  # Known before the app exists, so the app can be told its own address.
  public_url = "https://${local.app_name}.${azurerm_container_app_environment.main.default_domain}"
  image      = "${azurerm_container_registry.main.login_server}/libris-hosted:${var.image_tag}"
}

resource "azurerm_resource_group" "main" {
  name     = "rg-libris-${local.suffix}"
  location = var.location
}

resource "azurerm_user_assigned_identity" "app" {
  name                = "id-libris-mcp-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
}

# Registry ------------------------------------------------------------------

resource "azurerm_container_registry" "main" {
  name                = "acrlibris${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "Basic"
  admin_enabled       = false
}

resource "azurerm_role_assignment" "app_pulls_images" {
  scope                = azurerm_container_registry.main.id
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# Secrets -------------------------------------------------------------------

resource "azurerm_key_vault" "main" {
  name                       = "kv-libris-${local.suffix}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  tenant_id                  = data.azurerm_client_config.current.tenant_id
  sku_name                   = "standard"
  rbac_authorization_enabled = true
  soft_delete_retention_days = 7
}

resource "azurerm_role_assignment" "deployer_writes_secrets" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

resource "azurerm_role_assignment" "app_reads_secrets" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# A new role assignment takes a while to reach the data plane it grants, and
# depends_on waits only for the assignment to exist. Without this wait, the
# secret writes below fail with a 403, and on a clean deployment the Container
# App can try to read its Key Vault secrets or pull its image before its own
# roles work. So every role either side needs goes through this one wait.
resource "time_sleep" "secrets_role_propagates" {
  depends_on = [
    azurerm_role_assignment.deployer_writes_secrets,
    azurerm_role_assignment.app_reads_secrets,
    azurerm_role_assignment.app_pulls_images,
  ]
  create_duration = "60s"
}

resource "random_password" "token_signing_key" {
  length  = 64
  special = false
}

resource "azurerm_key_vault_secret" "token_signing_key" {
  name         = "token-signing-key"
  value        = random_password.token_signing_key.result
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [time_sleep.secrets_role_propagates]
}

resource "azurerm_key_vault_secret" "google_client_secret" {
  name         = "google-client-secret"
  value        = var.google_client_secret == "" ? "unset" : var.google_client_secret
  key_vault_id = azurerm_key_vault.main.id
  depends_on   = [time_sleep.secrets_role_propagates]
}

# Cosmos --------------------------------------------------------------------

resource "azurerm_cosmosdb_account" "main" {
  name                         = "cosmos-libris-${local.suffix}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  offer_type                   = "Standard"
  kind                         = "GlobalDocumentDB"
  local_authentication_enabled = false
  minimal_tls_version          = "Tls12"

  capabilities {
    name = "EnableServerless"
  }

  consistency_policy {
    consistency_level = "Session"
  }

  geo_location {
    location          = azurerm_resource_group.main.location
    failover_priority = 0
  }
}

resource "azurerm_cosmosdb_sql_database" "libris" {
  name                = "libris"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
}

# Registered clients and refresh tokens (ADR 0035). TTL is switched on with no
# default, so a refresh token document carries its own expiry.
resource "azurerm_cosmosdb_sql_container" "auth" {
  name                = "auth"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.libris.name
  partition_key_paths = ["/id"]
  default_ttl         = -1
}

locals {
  cosmos_data_contributor = "${azurerm_cosmosdb_account.main.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
}

resource "random_uuid" "app_cosmos_role" {}
resource "random_uuid" "deployer_cosmos_role" {}

resource "azurerm_cosmosdb_sql_role_assignment" "app" {
  name                = random_uuid.app_cosmos_role.result
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  role_definition_id  = local.cosmos_data_contributor
  principal_id        = azurerm_user_assigned_identity.app.principal_id
  scope               = azurerm_cosmosdb_account.main.id
}

# For `libris sync` from the PC (#173), which reaches Cosmos as the person's
# own Azure sign-in rather than through the hosted app.
resource "azurerm_cosmosdb_sql_role_assignment" "deployer" {
  name                = random_uuid.deployer_cosmos_role.result
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  role_definition_id  = local.cosmos_data_contributor
  principal_id        = data.azurerm_client_config.current.object_id
  scope               = azurerm_cosmosdb_account.main.id
}

# The app -------------------------------------------------------------------

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-libris-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
}

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-libris-${local.suffix}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
}

resource "azurerm_container_app" "mcp" {
  name                         = local.app_name
  container_app_environment_id = azurerm_container_app_environment.main.id
  resource_group_name          = azurerm_resource_group.main.name
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [azurerm_user_assigned_identity.app.id]
  }

  registry {
    server   = azurerm_container_registry.main.login_server
    identity = azurerm_user_assigned_identity.app.id
  }

  secret {
    name                = "token-signing-key"
    identity            = azurerm_user_assigned_identity.app.id
    key_vault_secret_id = azurerm_key_vault_secret.token_signing_key.versionless_id
  }

  secret {
    name                = "google-client-secret"
    identity            = azurerm_user_assigned_identity.app.id
    key_vault_secret_id = azurerm_key_vault_secret.google_client_secret.versionless_id
  }

  ingress {
    external_enabled = true
    target_port      = 8000
    transport        = "auto"

    traffic_weight {
      latest_revision = true
      percentage      = 100
    }
  }

  template {
    min_replicas = 0
    # One is plenty for one person. The app holds no sign-in state of its own
    # (ADR 0035), so raising this is safe; only the registration limit would
    # then count per replica.
    max_replicas = 1

    container {
      name   = "libris"
      image  = local.image
      cpu    = 0.25
      memory = "0.5Gi"

      env {
        name  = "LIBRIS_PUBLIC_URL"
        value = local.public_url
      }
      env {
        name  = "LIBRIS_GOOGLE_CLIENT_ID"
        value = var.google_client_id == "" ? "unset" : var.google_client_id
      }
      env {
        name        = "LIBRIS_GOOGLE_CLIENT_SECRET"
        secret_name = "google-client-secret"
      }
      env {
        name        = "LIBRIS_TOKEN_SIGNING_KEY"
        secret_name = "token-signing-key"
      }
      dynamic "env" {
        for_each = var.allowed_google_sub == "" ? [] : [var.allowed_google_sub]
        content {
          name  = "LIBRIS_ALLOWED_GOOGLE_SUB"
          value = env.value
        }
      }
      env {
        name  = "LIBRIS_COSMOS_ENDPOINT"
        value = azurerm_cosmosdb_account.main.endpoint
      }
      # Tells DefaultAzureCredential which of the app's identities to use.
      env {
        name  = "AZURE_CLIENT_ID"
        value = azurerm_user_assigned_identity.app.client_id
      }

      liveness_probe {
        transport = "HTTP"
        port      = 8000
        path      = "/healthz"
      }
    }
  }

  depends_on = [
    time_sleep.secrets_role_propagates,
    azurerm_cosmosdb_sql_role_assignment.app,
  ]
}
