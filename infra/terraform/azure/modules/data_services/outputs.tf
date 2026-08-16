output "acr_id" {
  value = azurerm_container_registry.this.id
}
output "acr_login_server" {
  value = azurerm_container_registry.this.login_server
}
output "servicebus_namespace_name" {
  value = azurerm_servicebus_namespace.this.name
}
output "servicebus_namespace_fqdn" {
  value = "${azurerm_servicebus_namespace.this.name}.servicebus.windows.net"
}

output "service_bus_id" {
  value = azurerm_servicebus_namespace.this.id
}

output "notification_queue_name" {
  value = azurerm_servicebus_queue.notification.name
}

output "notification_manual_queue_name" {
  value = azurerm_servicebus_queue.notification_manual.name
}

output "app_configuration_id" {
  value = azurerm_app_configuration.webhook_registry.id
}

output "app_configuration_endpoint" {
  value = azurerm_app_configuration.webhook_registry.endpoint
}