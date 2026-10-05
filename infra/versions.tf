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

  # Local state for the spike. It holds the token signing key and the Google
  # client secret, so it is git-ignored, and moving it to a storage account
  # backend is part of #175.
}

provider "azurerm" {
  features {}
  subscription_id = var.subscription_id
}
