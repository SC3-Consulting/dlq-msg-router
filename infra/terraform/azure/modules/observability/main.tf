resource "azurerm_log_analytics_workspace" "this" {
  name                = var.workspace_name
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "PerGB2018"
  retention_in_days   = 30
  tags                = var.tags
}

data "azurerm_client_config" "current" {}

resource "random_uuid" "workbook" {}

resource "azapi_resource" "workbook" {
  type      = "Microsoft.Insights/workbooks@2022-04-01"
  name      = random_uuid.workbook.result
  parent_id = "/subscriptions/${data.azurerm_client_config.current.subscription_id}/resourceGroups/${var.resource_group_name}"
  location  = var.location
  tags      = var.tags

  body = {
    kind = "shared"
    properties = {
      displayName = "DLQ Operations Workbook"
      sourceId    = azurerm_log_analytics_workspace.this.id
      version     = "1.0"
      category    = "workbook"
      serializedData = jsonencode({
        version = "Notebook/1.0"
        items = [
          {
            type = 1
            content = {
              json = jsonencode({
                title = "DLQ Operations"
                text  = "This Azure Monitor workbook is the Azure-native baseline for the DLQ pipeline. It reads from the shared Log Analytics workspace and is deployed with Terraform as part of the environment. The local jumpbox-hosted Grafana remains a sandbox convenience layer for local experimentation and advanced visual iteration."
                style = "info"
              })
            }
          }
        ]
      })
    }
  }
}