resource "azurerm_cognitive_account" "foundry" {
  name                          = "foundry-viva-swastik-99"
  location                      = var.location
  resource_group_name           = var.resource_group_name
  kind                          = "AIServices"
  sku_name                      = "S0"
  public_network_access_enabled = false
  custom_subdomain_name         = "foundry-viva-swastik-99"
}


# Deploy the primary query model (e.g., gpt-4o-mini or gpt-5.1-chat)
resource "azapi_resource" "model_deployment_query" {
  count                     = var.enable_model_deployments ? 1 : 0
  type                      = "Microsoft.CognitiveServices/accounts/deployments@2025-06-01"
  name                      = var.query_model.name
  parent_id                 = azurerm_cognitive_account.foundry.id
  schema_validation_enabled = false

  body = {
    sku = {
      name     = "GlobalStandard"
      capacity = var.query_model.capacity
    }
    properties = {
      model = {
        format  = "OpenAI"
        name    = var.query_model.name
        version = var.query_model.version
      }
    }
  }
}