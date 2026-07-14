resource "azurerm_container_registry" "this" {
  name                          = "acrmsgrouter99"
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
  name         = "integration-queue"
  namespace_id = azurerm_servicebus_namespace.this.id
  dead_lettering_on_message_expiration = true
  max_delivery_count                   = 10
}

resource "azurerm_servicebus_queue" "payment" {
  name         = "payments-queue"
  namespace_id = azurerm_servicebus_namespace.this.id
  dead_lettering_on_message_expiration = true
  max_delivery_count                   = 10
}