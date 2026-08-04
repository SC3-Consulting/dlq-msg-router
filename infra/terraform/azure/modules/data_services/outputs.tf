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