terraform {
  required_version = ">= 1.9"

  required_providers {
    azurerm = {
      source  = "hashicorp/azurerm"
      version = "~> 5.8"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
    time = {
      source  = "hashicorp/time"
      version = "~> 0.12"
    }
  }

  # The state holds the token signing key and the Google client secret, so it
  # lives in a storage account that takes no access keys, reached as whoever
  # runs Terraform: the deploy workflow's identity or your az login (#175).
  # infra/bootstrap creates the account; its suffix is the deployment's.
  backend "azurerm" {
    resource_group_name  = "rg-libris-tfstate-bacndh"
    storage_account_name = "stlibristfbacndh"
    container_name       = "tfstate"
    key                  = "libris.tfstate"
    use_azuread_auth     = true
  }
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
  # The deploy workflow's identity holds roles on the resource group only, and
  # registering a provider is a subscription-wide action it is refused.
  resource_provider_registrations = "none"
}
