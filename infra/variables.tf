variable "subscription_id" {
  description = "The Azure subscription to deploy into."
  type        = string
}

variable "location" {
  description = "The Azure region for every resource."
  type        = string
  default     = "eastus2"
}

variable "google_client_id" {
  description = "The Google OAuth client Libris signs people in through (ADR 0035). Empty until it exists."
  type        = string
  default     = ""
}

variable "google_client_secret" {
  description = "That client's secret. Written to Key Vault; never set it in a committed file."
  type        = string
  default     = ""
  sensitive   = true
}

variable "allowed_google_sub" {
  description = "The one Google account allowed in. Empty on the first deployment: the refused sign-in shows the value to put here."
  type        = string
  default     = ""
}

variable "image_tag" {
  description = "The libris-hosted image tag in the registry to run."
  type        = string
  default     = "spike"
}
