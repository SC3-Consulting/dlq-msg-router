resource "random_string" "acr_suffix" {
  length  = 6
  upper   = false
  lower   = true
  numeric = true
  special = false

  keepers = {
    environment = var.suffix
    seed        = var.acr_name_seed
  }
}

locals {
  default_acr_name = substr(
    lower("acrdlqmsgrouter${var.suffix}${random_string.acr_suffix.result}"),
    0,
    50,
  )
  acr_name = trimspace(var.acr_name_override) != "" ? lower(trimspace(var.acr_name_override)) : local.default_acr_name
}

resource "azurerm_container_registry" "this" {
  name                          = local.acr_name
  resource_group_name           = var.resource_group_name
  location                      = var.location
  sku                           = "Premium"
  admin_enabled                 = false
  public_network_access_enabled = false
}

resource "random_string" "servicebus_suffix" {
  length  = 6
  upper   = false
  lower   = true
  numeric = true
  special = false

  keepers = {
    environment = var.suffix
    seed        = var.servicebus_name_seed
  }
}

locals {
  servicebus_name = substr(
    lower("sb-dlq-msg-router-${var.suffix}-${random_string.servicebus_suffix.result}"),
    0,
    50,
  )
}

resource "azurerm_servicebus_namespace" "this" {
  name                          = local.servicebus_name
  location                      = var.location
  resource_group_name           = var.resource_group_name
  sku                           = "Premium"
  capacity                      = 1
  premium_messaging_partitions  = 1
  public_network_access_enabled = false
}


# --- YOUR PROJECT QUEUES AUTOMATICALLY BUILT ---

resource "azurerm_servicebus_queue" "parking_lot" {
  name         = "parking-lot-queue"
  namespace_id = azurerm_servicebus_namespace.this.id
}

resource "azurerm_servicebus_queue" "integration" {
  name                                 = "integration-queue"
  namespace_id                         = azurerm_servicebus_namespace.this.id
  dead_lettering_on_message_expiration = true
  max_delivery_count                   = 10
}

resource "azurerm_servicebus_queue" "payment" {
  name                                 = "payments-queue"
  namespace_id                         = azurerm_servicebus_namespace.this.id
  dead_lettering_on_message_expiration = true
  max_delivery_count                   = 10
}

resource "azurerm_servicebus_queue" "notification" {
  name                                    = "notification-queue"
  namespace_id                            = azurerm_servicebus_namespace.this.id
  dead_lettering_on_message_expiration    = true
  default_message_ttl                     = "P7D"
  max_delivery_count                      = 10
  requires_duplicate_detection            = true
  duplicate_detection_history_time_window = "PT10M"
}

resource "azurerm_servicebus_queue" "notification_manual" {
  name                                    = "notification-manual-queue"
  namespace_id                            = azurerm_servicebus_namespace.this.id
  dead_lettering_on_message_expiration    = true
  default_message_ttl                     = "P30D"
  max_delivery_count                      = 10
  requires_duplicate_detection            = true
  duplicate_detection_history_time_window = "PT10M"
}

resource "azurerm_app_configuration" "webhook_registry" {
  name                  = substr(lower("appcfg-dlq-${var.suffix}-${random_string.servicebus_suffix.result}"), 0, 50)
  resource_group_name   = var.resource_group_name
  location              = var.location
  sku                   = "standard"
  public_network_access = "Disabled"
  tags                  = var.tags
}

resource "azurerm_role_assignment" "app_configuration_data_owner" {
  scope                = azurerm_app_configuration.webhook_registry.id
  role_definition_name = "App Configuration Data Owner"
  principal_id         = var.app_configuration_data_owner_principal_id
}

resource "azurerm_role_assignment" "app_configuration_contributor" {
  scope                = azurerm_app_configuration.webhook_registry.id
  role_definition_name = "App Configuration Contributor"
  principal_id         = var.app_configuration_management_principal_id
}

resource "azurerm_app_configuration_key" "webhook_registry" {
  for_each = length(var.webhook_registry) > 0 ? { registry = var.webhook_registry } : {}

  depends_on = [
    azurerm_role_assignment.app_configuration_data_owner,
    azurerm_role_assignment.app_configuration_contributor,
  ]

  configuration_store_id = azurerm_app_configuration.webhook_registry.id
  key                    = "webhook-registry"
  type                   = "kv"
  value                  = jsonencode(var.webhook_registry)
  content_type           = "application/json"
  label                  = var.suffix
}