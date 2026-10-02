output "mcp_url" {
  description = "What to paste into Claude and Gemini as the connector URL."
  value       = "${local.public_url}/mcp"
}

output "google_redirect_uri" {
  description = "The authorized redirect URI to register on the Google OAuth client."
  value       = "${local.public_url}/oauth/google/callback"
}

output "registry" {
  description = "The registry to build the image into."
  value       = azurerm_container_registry.main.name
}

output "resource_group" {
  value = azurerm_resource_group.main.name
}
