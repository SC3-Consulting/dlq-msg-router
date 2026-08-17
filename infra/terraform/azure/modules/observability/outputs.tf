output "log_analytics_workspace_id" {
  value = azurerm_log_analytics_workspace.this.id
}

output "workbook_id" {
  value = azapi_resource.workbook.id
}