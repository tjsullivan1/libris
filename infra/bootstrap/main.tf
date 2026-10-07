# What has to exist before infra/ can run from GitHub Actions (#175): a storage
# account for its state, and an identity the deploy workflow signs in as. Run
# once, by hand. Its own state is local and git-ignored, and holds no secret:
# the identity signs in through GitHub's OIDC token, so there is no password.

terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.8"
    }
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
  # The account takes no access keys, so its data plane is reached through Entra.
  storage_use_azuread = true

  # infra/ registers nothing: the deploy identity's roles stop at its resource
  # group, and registering is subscription-wide. So on a clean subscription
  # this is where the providers both roots use get registered, by a person
  # who can. Registering one that already is changes nothing.
  resource_provider_registrations = "none"
  resource_providers_to_register = [
    "Microsoft.App",
    "Microsoft.ContainerRegistry",
    "Microsoft.DocumentDB",
    "Microsoft.KeyVault",
    "Microsoft.ManagedIdentity",
    "Microsoft.OperationalInsights",
    "Microsoft.Storage",
  ]
}

variable "subscription_id" {
  description = "The Azure subscription to deploy into. Leave unset to use ARM_SUBSCRIPTION_ID from the environment."
  type        = string
  default     = null
}

variable "suffix" {
  description = "The suffix infra/ deploys with (`terraform -chdir=infra output -raw suffix`)."
  type        = string

  # "stlibristf" leaves 14 of a storage account name's 24 characters, the same
  # limit infra/ puts on it.
  validation {
    condition     = can(regex("^[a-z0-9]{1,14}$", var.suffix))
    error_message = "The suffix must be 1-14 lowercase letters or digits."
  }
}

variable "location" {
  description = "The Azure region for both resource groups."
  type        = string
  default     = "eastus2"
}

variable "github_repository" {
  description = "The repository whose deploy workflow may sign in, as owner/name."
  type        = string
  default     = "tjsullivan1/libris"
}

data "azurerm_client_config" "current" {}

resource "azurerm_resource_group" "deploy" {
  name     = "rg-libris-tfstate-${var.suffix}"
  location = var.location
}

# State --------------------------------------------------------------------

resource "azurerm_storage_account" "state" {
  name                            = "stlibristf${var.suffix}"
  resource_group_name             = azurerm_resource_group.deploy.name
  location                        = azurerm_resource_group.deploy.location
  account_tier                    = "Standard"
  account_replication_type        = "LRS"
  min_tls_version                 = "TLS1_2"
  shared_access_key_enabled       = false
  default_to_oauth_authentication = true
  allow_nested_items_to_be_public = false

  # The state holds the token signing key; a version kept for a week undoes a
  # bad write to it.
  blob_properties {
    versioning_enabled = true
    delete_retention_policy {
      days = 7
    }
  }
}

resource "azurerm_storage_container" "state" {
  name               = "tfstate"
  storage_account_id = azurerm_storage_account.state.id
}

# Whoever applies infra/ by hand reads and writes the same state as CI.
resource "azurerm_role_assignment" "owner_state" {
  scope                = azurerm_storage_container.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = data.azurerm_client_config.current.object_id
}

# The deploy workflow's identity ------------------------------------------

resource "azurerm_user_assigned_identity" "deploy" {
  name                = "id-libris-deploy-${var.suffix}"
  location            = azurerm_resource_group.deploy.location
  resource_group_name = azurerm_resource_group.deploy.name
}

# Only a job running in the repository's `production` environment gets a token
# this accepts. The environment, in turn, admits only `main`
# (infra/README.md), so neither a pull request nor a branch can deploy.
resource "azurerm_federated_identity_credential" "github" {
  name                      = "github-production"
  user_assigned_identity_id = azurerm_user_assigned_identity.deploy.id
  issuer                    = "https://token.actions.githubusercontent.com"
  audience                  = ["api://AzureADTokenExchange"]
  subject                   = "repo:${var.github_repository}:environment:production"
}

resource "azurerm_role_assignment" "deploy_state" {
  scope                = azurerm_storage_container.state.id
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.deploy.principal_id
}

# Its roles on the app's resource group are granted by infra/ itself
# (deploy_principal_id), which creates that group. Granting them here would
# need the group to exist before the state it is recorded in.

# What the workflow needs, set as GitHub variables (infra/README.md).

output "azure_client_id" {
  value = azurerm_user_assigned_identity.deploy.client_id
}

output "azure_tenant_id" {
  value = data.azurerm_client_config.current.tenant_id
}

output "azure_subscription_id" {
  value = data.azurerm_client_config.current.subscription_id
}

output "deploy_principal_id" {
  description = "For infra/'s deploy_principal_id."
  value       = azurerm_user_assigned_identity.deploy.principal_id
}

output "owner_object_id" {
  description = "Your own object ID, for infra/'s owner_object_id."
  value       = data.azurerm_client_config.current.object_id
}
