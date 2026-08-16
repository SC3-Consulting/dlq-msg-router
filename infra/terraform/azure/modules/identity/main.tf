data "azurerm_client_config" "current" {}

resource "azurerm_user_assigned_identity" "agent_runtime" {
  name                = trimspace(var.agent_runtime_identity_name_override) != "" ? var.agent_runtime_identity_name_override : "id-agent-runtime-${var.suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_user_assigned_identity" "notification_runtime" {
  name                = "id-notification-${var.suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  tags                = var.tags
}

resource "azurerm_role_assignment" "cognitive_services_user" {
  scope                = var.scope_ids.foundry
  role_definition_name = "Cognitive Services User"
  principal_id         = azurerm_user_assigned_identity.agent_runtime.principal_id
}

resource "azurerm_role_assignment" "service_bus_data_owner" {
  scope                = var.scope_ids.service_bus
  role_definition_name = "Azure Service Bus Data Owner"
  principal_id         = azurerm_user_assigned_identity.agent_runtime.principal_id
}

resource "azurerm_role_assignment" "acr_pull" {
  scope                = var.scope_ids.acr
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.agent_runtime.principal_id
}

resource "azurerm_role_assignment" "acr_push" {
  scope                = var.scope_ids.acr
  role_definition_name = "AcrPush"
  principal_id         = azurerm_user_assigned_identity.agent_runtime.principal_id
}

resource "azurerm_role_assignment" "log_analytics_contributor" {
  scope                = var.scope_ids.log_analytics
  role_definition_name = "Log Analytics Contributor"
  principal_id         = azurerm_user_assigned_identity.agent_runtime.principal_id
}

resource "azurerm_role_assignment" "state_blob_data_contributor" {
  scope                = var.scope_ids.state_backend
  role_definition_name = "Storage Blob Data Contributor"
  principal_id         = azurerm_user_assigned_identity.agent_runtime.principal_id
}

# Allow Jumpbox VM to read resources in the RG
resource "azurerm_role_assignment" "agent_runtime_rg_reader" {
  scope                = "/subscriptions/${data.azurerm_client_config.current.subscription_id}/resourceGroups/${var.resource_group_name}"
  role_definition_name = "Reader"
  principal_id         = azurerm_user_assigned_identity.agent_runtime.principal_id
}

resource "azurerm_role_assignment" "notification_service_bus_receiver" {
  scope                = var.scope_ids.service_bus
  role_definition_name = "Azure Service Bus Data Receiver"
  principal_id         = azurerm_user_assigned_identity.notification_runtime.principal_id
}

resource "azurerm_role_assignment" "notification_service_bus_sender" {
  scope                = var.scope_ids.service_bus
  role_definition_name = "Azure Service Bus Data Sender"
  principal_id         = azurerm_user_assigned_identity.notification_runtime.principal_id
}

resource "azurerm_role_assignment" "notification_app_configuration_reader" {
  scope                = var.scope_ids.app_configuration
  role_definition_name = "App Configuration Data Reader"
  principal_id         = azurerm_user_assigned_identity.notification_runtime.principal_id
}

resource "azurerm_role_assignment" "jumpbox_app_configuration_reader" {
  scope                = var.scope_ids.app_configuration
  role_definition_name = "App Configuration Data Owner"
  principal_id         = azurerm_user_assigned_identity.agent_runtime.principal_id
}

resource "azurerm_role_assignment" "jumpbox_app_configuration_contributor" {
  scope                = var.scope_ids.app_configuration
  role_definition_name = "App Configuration Contributor"
  principal_id         = azurerm_user_assigned_identity.agent_runtime.principal_id
}

resource "azurerm_role_assignment" "notification_key_vault_secrets_user" {
  scope                = var.scope_ids.webhook_key_vault
  role_definition_name = "Key Vault Secrets User"
  principal_id         = azurerm_user_assigned_identity.notification_runtime.principal_id
}

resource "azurerm_role_assignment" "jumpbox_webhook_secrets_officer" {
  scope                = var.scope_ids.webhook_key_vault
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = azurerm_user_assigned_identity.agent_runtime.principal_id
}

resource "azurerm_role_assignment" "notification_acr_pull" {
  scope                = var.scope_ids.acr
  role_definition_name = "AcrPull"
  principal_id         = azurerm_user_assigned_identity.notification_runtime.principal_id
}