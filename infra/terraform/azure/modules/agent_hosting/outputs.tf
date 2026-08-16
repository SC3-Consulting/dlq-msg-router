output "container_app_id" {
  value = azurerm_container_app.dlq_agent.id
}

output "container_app_environment_id" {
  value = azurerm_container_app_environment.this.id
}

output "notification_container_app_id" {
  value = azurerm_container_app.notification_worker.id
}