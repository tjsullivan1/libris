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

# The deploy workflow (#175) applies this configuration, which grants roles of
# its own, so Contributor is not enough. Scoped to this one group. The first
# apply, by hand, is what lets the workflow run at all.
resource "azurerm_role_assignment" "deploy_owns_group" {
  scope                = azurerm_resource_group.main.id
  role_definition_name = "Owner"
  principal_id         = var.deploy_principal_id
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

# For an apply by hand; the deploy workflow's identity has its own, below.
# Neither is keyed to whoever is running Terraform: that would
# swap the assignment between the two on every apply, and an apply from CI
# would take away the owner's own access (#175).
resource "azurerm_role_assignment" "owner_writes_secrets" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = var.owner_object_id
}

moved {
  from = azurerm_role_assignment.deployer_writes_secrets
  to   = azurerm_role_assignment.owner_writes_secrets
}

# Owner does not reach a Key Vault's data plane.
resource "azurerm_role_assignment" "deploy_writes_secrets" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = var.deploy_principal_id
}

resource "azurerm_role_assignment" "app_reads_secrets" {
  scope                = azurerm_key_vault.main.id
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# A new role assignment takes a while to reach the data plane it grants, and
# depends_on waits only for the assignment to exist. Without this wait, the
# secret writes below fail with a 403, and on a clean deployment the Container
# App can try to read its Key Vault secrets, pull its image or reach Cosmos
# before its own roles work. So every role either side needs goes through
# this one wait.
resource "time_sleep" "secrets_role_propagates" {
  depends_on = [
    azurerm_role_assignment.owner_writes_secrets,
    azurerm_role_assignment.deploy_writes_secrets,
    azurerm_role_assignment.app_reads_secrets,
    azurerm_role_assignment.app_pulls_images,
    azurerm_cosmosdb_sql_role_assignment.app,
    azurerm_role_assignment.app_sends_telemetry,
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

# Optional, unlike the secrets above: Google Books answers without a key, and a
# placeholder would be sent as one and refused. So with no key there is no
# secret, and the app runs keyless (#175).
resource "azurerm_key_vault_secret" "google_books_api_key" {
  count        = var.google_books_api_key == "" ? 0 : 1
  name         = "google-books-api-key"
  value        = var.google_books_api_key
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

  # Azure's default, written out so it is a decision rather than an accident
  # (#179). Almost nothing here needs a backup: the books are rebuilt from the
  # Shelf by sync, and lost sign-ins only mean signing in again. Pending Intents
  # are the exception, and this covers them through a support ticket.
  # Continuous backup was considered and left out: the switch is one-way, and
  # it would protect a few hours of Intents (infra/README.md).
  backup {
    type                = "Periodic"
    interval_in_minutes = 240
    retention_in_hours  = 8
    storage_redundancy  = "Geo"
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

# One document per Book Note, keyed and partitioned by Libris ID (ADR 0006),
# written by `libris sync` (#173). Only the keys a store is queried on are
# indexed: the frontmatter and the body are carried, never searched, and
# indexing them would bill every push for paths no query reads.
resource "azurerm_cosmosdb_sql_container" "books" {
  name                = "books"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.libris.name
  partition_key_paths = ["/id"]

  indexing_policy {
    indexing_mode = "consistent"

    included_path {
      path = "/*"
    }
    excluded_path {
      path = "/frontmatter/*"
    }
    excluded_path {
      path = "/body/?"
    }

    # A listing orders by title and breaks ties on the Libris ID (ADR 0033).
    # Cosmos refuses an ORDER BY on two paths without an index over both. The
    # title is ordered through `title_order`, an ASCII spelling of it, so the
    # order does not depend on how Cosmos compares Unicode strings.
    composite_index {
      index {
        path  = "/title_order"
        order = "Ascending"
      }
      index {
        path  = "/libris_id"
        order = "Ascending"
      }
    }
  }
}

# The word counts per Status, one document rebuilt at every sync (ADR 0033).
# Read by id only, so the counts themselves - about 8,000 words a bucket - are
# left out of the index.
resource "azurerm_cosmosdb_sql_container" "word_counts" {
  name                = "word_counts"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.libris.name
  partition_key_paths = ["/id"]

  indexing_policy {
    indexing_mode = "consistent"

    included_path {
      path = "/*"
    }
    excluded_path {
      path = "/buckets/*"
    }
  }
}

# Changes a Surface with no Shelf records for `libris sync` to apply (ADR 0002,
# #176): pending until sync settles them, then kept as a record of what each
# Surface did (ADR 0006). A handful of small documents, queried by kind and
# whether they are pending, so the default index costs nothing worth trimming.
# The app and the owner reach it through the account-wide roles below.
resource "azurerm_cosmosdb_sql_container" "intents" {
  name                = "intents"
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  database_name       = azurerm_cosmosdb_sql_database.libris.name
  partition_key_paths = ["/id"]
}

locals {
  cosmos_data_contributor = "${azurerm_cosmosdb_account.main.id}/sqlRoleDefinitions/00000000-0000-0000-0000-000000000002"
}

resource "random_uuid" "app_cosmos_role" {}
resource "random_uuid" "owner_cosmos_role" {}

moved {
  from = random_uuid.deployer_cosmos_role
  to   = random_uuid.owner_cosmos_role
}

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
resource "azurerm_cosmosdb_sql_role_assignment" "owner" {
  name                = random_uuid.owner_cosmos_role.result
  resource_group_name = azurerm_resource_group.main.name
  account_name        = azurerm_cosmosdb_account.main.name
  role_definition_id  = local.cosmos_data_contributor
  principal_id        = var.owner_object_id
  scope               = azurerm_cosmosdb_account.main.id
}

moved {
  from = azurerm_cosmosdb_sql_role_assignment.deployer
  to   = azurerm_cosmosdb_sql_role_assignment.owner
}

# The app -------------------------------------------------------------------

resource "azurerm_log_analytics_workspace" "main" {
  name                = "log-libris-${local.suffix}"
  location            = azurerm_resource_group.main.location
  resource_group_name = azurerm_resource_group.main.name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  # A ceiling, not an estimate: one person's traffic is megabytes a day. A
  # loop logging on every request is what this stops from becoming a bill
  # (#179). Ingestion past it is dropped until the next UTC day.
  daily_quota_gb = 0.2
}

# Traces of every request and tool call (#179), kept in the workspace above.
# Accepts telemetry only from an Entra identity, like everything else here
# (ADR 0006): the connection string says where to send, not who may.
resource "azurerm_application_insights" "main" {
  name                         = "appi-libris-${local.suffix}"
  location                     = azurerm_resource_group.main.location
  resource_group_name          = azurerm_resource_group.main.name
  workspace_id                 = azurerm_log_analytics_workspace.main.id
  application_type             = "other"
  local_authentication_enabled = false
}

resource "azurerm_role_assignment" "app_sends_telemetry" {
  scope                = azurerm_application_insights.main.id
  role_definition_name = "Monitoring Metrics Publisher"
  principal_id         = azurerm_user_assigned_identity.app.principal_id
}

# Cost --------------------------------------------------------------------------

# Mails the owner before a month's costs surprise them (#179). Most of the
# spend is the registry's flat daily fee, so the amount leaves room above it:
# an alert means something changed, not that a month went by.
resource "azurerm_consumption_budget_resource_group" "main" {
  name              = "budget-libris-${local.suffix}"
  resource_group_id = azurerm_resource_group.main.id
  amount            = var.monthly_budget
  time_grain        = "Monthly"

  time_period {
    # Must be the first of a month. Fixed, so a later apply never moves it.
    start_date = "2026-11-01T00:00:00Z"
  }

  notification {
    threshold      = 80
    threshold_type = "Actual"
    operator       = "GreaterThanOrEqualTo"
    contact_emails = [var.budget_alert_email]
  }

  notification {
    threshold      = 100
    threshold_type = "Actual"
    operator       = "GreaterThanOrEqualTo"
    contact_emails = [var.budget_alert_email]
  }

  # Warns mid-month, while there is still time to act on it.
  notification {
    threshold      = 100
    threshold_type = "Forecasted"
    operator       = "GreaterThanOrEqualTo"
    contact_emails = [var.budget_alert_email]
  }
}

resource "azurerm_container_app_environment" "main" {
  name                       = "cae-libris-${local.suffix}"
  location                   = azurerm_resource_group.main.location
  resource_group_name        = azurerm_resource_group.main.name
  logs_destination           = "log-analytics"
  log_analytics_workspace_id = azurerm_log_analytics_workspace.main.id
}

# Created only once the registry holds an image for it, which is what the first
# deployment's two applies are about (infra/README.md).
resource "azurerm_container_app" "mcp" {
  count                        = var.deploy_app ? 1 : 0
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

  dynamic "secret" {
    for_each = azurerm_key_vault_secret.google_books_api_key
    content {
      name                = "google-books-api-key"
      identity            = azurerm_user_assigned_identity.app.id
      key_vault_secret_id = secret.value.versionless_id
    }
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
      dynamic "env" {
        for_each = azurerm_key_vault_secret.google_books_api_key
        content {
          name        = "LIBRIS_GOOGLE_BOOKS_API_KEY"
          secret_name = "google-books-api-key"
        }
      }
      env {
        name  = "LIBRIS_COSMOS_ENDPOINT"
        value = azurerm_cosmosdb_account.main.endpoint
      }
      # Switches tracing on (#179). Not a secret: the resource refuses
      # anything but the app's identity, so it only says where to send.
      env {
        name  = "APPLICATIONINSIGHTS_CONNECTION_STRING"
        value = azurerm_application_insights.main.connection_string
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

  depends_on = [time_sleep.secrets_role_propagates]

  # The app can't sign anyone in without Google's credentials. An apply from a
  # shell that lacked TF_VAR_google_client_id once deployed the "unset"
  # placeholders, and every sign-in then died at Google with invalid_client.
  # Checked only when this resource is planned, so the first deployment's
  # targeted apply, before the Google client exists, still works.
  lifecycle {
    precondition {
      condition     = var.google_client_id != "" && var.google_client_secret != ""
      error_message = "Set google_client_id and google_client_secret (in terraform.tfvars, or TF_VAR_google_client_id and TF_VAR_google_client_secret) before deploying the app. Without them nobody can sign in."
    }

    # The deploy workflow moves the app to each commit's image. Terraform sets
    # the image only when it creates the app, so an apply never rolls back
    # what the workflow deployed.
    ignore_changes = [template[0].container[0].image]
  }
}

moved {
  from = azurerm_container_app.mcp
  to   = azurerm_container_app.mcp[0]
}
