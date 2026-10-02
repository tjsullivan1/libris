variable "subscription_id" {
  description = "The Azure subscription to deploy into. Leave unset to use ARM_SUBSCRIPTION_ID from the environment."
  type        = string
  default     = null
}

variable "suffix" {
  description = "Appended to every resource name to keep globally unique names unique. Leave unset to generate one."
  type        = string
  default     = null

  # Key Vault names stop at 24 characters and ACR names allow letters and
  # digits only, so the suffix has to fit both: "kv-libris-" leaves 14.
  validation {
    condition     = var.suffix == null || can(regex("^[a-z0-9]{1,14}$", var.suffix))
    error_message = "The suffix must be 1-14 lowercase letters or digits."
  }
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
