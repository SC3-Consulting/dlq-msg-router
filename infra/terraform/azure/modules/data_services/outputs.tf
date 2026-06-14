output "acr_id" {
  value = azurerm_container_registry.this.id
}
output "acr_login_server" {
  value = azurerm_container_registry.this.login_server
}
output "service_bus_id" {
  value = azurerm_servicebus_namespace.this.id
}