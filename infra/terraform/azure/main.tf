module "foundation" {
  source              = "./modules/foundation"
  resource_group_name = var.resource_group_name
  location            = var.location
}

data "azurerm_client_config" "current" {}

locals {
  jumpbox_ssh_public_key_secret_name = "jumpbox-admin-ssh-public-key-${var.environment}"
  use_key_vault_jumpbox_key          = trimspace(var.bootstrap_key_vault_name) != ""
}

data "azurerm_key_vault" "bootstrap" {
  count               = local.use_key_vault_jumpbox_key ? 1 : 0
  name                = var.bootstrap_key_vault_name
  resource_group_name = var.bootstrap_key_vault_resource_group_name
}

data "azurerm_key_vault_secret" "jumpbox_admin_ssh_public_key" {
  count        = local.use_key_vault_jumpbox_key ? 1 : 0
  name         = local.jumpbox_ssh_public_key_secret_name
  key_vault_id = data.azurerm_key_vault.bootstrap[0].id
}

module "network" {
  source                       = "./modules/network"
  resource_group_name          = module.foundation.resource_group_name
  location                     = var.location
  vnet_name                    = "vnet-${var.environment}"
  vnet_cidr                    = var.vnet_cidr
  private_endpoint_subnet_cidr = var.private_endpoint_subnet_cidr
  agent_subnet_cidr            = var.agent_subnet_cidr
  container_apps_subnet_cidr   = var.container_apps_subnet_cidr
  jumpbox_subnet_cidr          = var.jumpbox_subnet_cidr
  azure_bastion_subnet_cidr    = var.azure_bastion_subnet_cidr
}

module "dns" {
  source              = "./modules/dns"
  resource_group_name = module.foundation.resource_group_name
  location            = var.location
  vnet_id             = module.network.vnet_id
}

module "observability" {
  source              = "./modules/observability"
  resource_group_name = module.foundation.resource_group_name
  location            = var.location
  workspace_name      = "law-${var.environment}"
}

module "data_services" {
  source                                    = "./modules/data_services"
  resource_group_name                       = module.foundation.resource_group_name
  location                                  = var.location
  suffix                                    = var.environment
  webhook_registry                          = var.webhook_registry
  app_configuration_data_owner_principal_id = local.app_configuration_deployer_object_id
  app_configuration_management_principal_id = local.app_configuration_deployer_object_id
}

locals {
  app_configuration_deployer_object_id = trimspace(var.app_configuration_deployer_object_id) != "" ? var.app_configuration_deployer_object_id : data.azurerm_client_config.current.object_id
}

resource "random_string" "webhook_vault_suffix" {
  length  = 6
  upper   = false
  lower   = true
  numeric = true
  special = false

  keepers = {
    environment = var.environment
  }
}

locals {
  webhook_vault_name = trimspace(var.webhook_secrets_vault_name) != "" ? var.webhook_secrets_vault_name : substr(lower("kvwebhook${var.environment}${random_string.webhook_vault_suffix.result}"), 0, 24)
}

resource "azurerm_key_vault" "webhook_secrets" {
  name                          = local.webhook_vault_name
  location                      = var.location
  resource_group_name           = module.foundation.resource_group_name
  tenant_id                     = data.azurerm_client_config.current.tenant_id
  sku_name                      = "standard"
  rbac_authorization_enabled    = true
  purge_protection_enabled      = true
  soft_delete_retention_days    = 90
  public_network_access_enabled = false
  tags                          = var.tags
}

resource "azurerm_role_assignment" "webhook_secrets_deployer" {
  scope                = azurerm_key_vault.webhook_secrets.id
  role_definition_name = "Key Vault Secrets Officer"
  principal_id         = data.azurerm_client_config.current.object_id
}

module "foundry" {
  source                    = "./modules/foundry"
  resource_group_name       = module.foundation.resource_group_name
  location                  = var.location
  suffix                    = var.environment
  query_model               = var.query_model
  delegated_agent_subnet_id = module.network.agent_subnet_id
  enable_model_deployments  = true
}

module "private_endpoints" {
  source                     = "./modules/private_endpoints"
  resource_group_name        = module.foundation.resource_group_name
  location                   = var.location
  private_endpoint_subnet_id = module.network.private_endpoint_subnet_id
  private_dns_zone_ids       = module.dns.private_dns_zone_ids
  foundry_account_id         = module.foundry.foundry_account_id
  foundry_account_name       = module.foundry.foundry_account_name
  acr_id                     = module.data_services.acr_id
  service_bus_id             = module.data_services.service_bus_id
  app_configuration_id       = module.data_services.app_configuration_id
  webhook_key_vault_id       = azurerm_key_vault.webhook_secrets.id
}

module "identity" {
  source              = "./modules/identity"
  resource_group_name = module.foundation.resource_group_name
  location            = var.location
  suffix              = var.environment
  scope_ids = {
    foundry           = module.foundry.foundry_account_id
    acr               = module.data_services.acr_id
    service_bus       = module.data_services.service_bus_id
    log_analytics     = module.observability.log_analytics_workspace_id
    state_backend     = data.azurerm_storage_account.state_backend.id
    app_configuration = module.data_services.app_configuration_id
    webhook_key_vault = azurerm_key_vault.webhook_secrets.id
  }
}

data "azurerm_storage_account" "state_backend" {
  name                = regex("storage_account_name\\s*=\\s*\"([^\"]+)\"", file("${path.module}/environments/${var.environment}/backend.hcl"))[0]
  resource_group_name = regex("resource_group_name\\s*=\\s*\"([^\"]+)\"", file("${path.module}/environments/${var.environment}/backend.hcl"))[0]
}

module "bastion_jumpbox" {
  source                       = "./modules/bastion_jumpbox"
  depends_on                   = [module.network]
  resource_group_name          = module.foundation.resource_group_name
  location                     = var.location
  jumpbox_subnet_id            = module.network.jumpbox_subnet_id
  azure_bastion_subnet_id      = module.network.azure_bastion_subnet_id
  suffix                       = var.environment
  jumpbox_admin_ssh_public_key = local.use_key_vault_jumpbox_key ? data.azurerm_key_vault_secret.jumpbox_admin_ssh_public_key[0].value : var.jumpbox_admin_ssh_public_key
  jumpbox_vm_size              = var.jumpbox_vm_size
  agent_runtime_identity_id    = module.identity.agent_runtime_identity_id
}

data "azurerm_user_assigned_identity" "agent" {
  name                = "id-agent-runtime-${var.environment}"
  resource_group_name = module.foundation.resource_group_name
  depends_on          = [module.identity]
}

data "azurerm_key_vault_secret" "app_env_secret" {
  for_each     = local.use_key_vault_jumpbox_key ? var.app_secret_env_var_secret_names : {}
  name         = each.value
  key_vault_id = data.azurerm_key_vault.bootstrap[0].id
}

locals {
  app_secret_env_var_secret_ids = {
    for env_name, secret_name in var.app_secret_env_var_secret_names :
    env_name => data.azurerm_key_vault_secret.app_env_secret[env_name].id
    if local.use_key_vault_jumpbox_key
  }
}
module "agent_hosting" {
  source                     = "./modules/agent_hosting"
  resource_group_name        = module.foundation.resource_group_name
  location                   = var.location
  suffix                     = var.environment
  delegated_agent_subnet_id  = module.network.container_apps_subnet_id
  log_analytics_workspace_id = module.observability.log_analytics_workspace_id
  acr_login_server           = module.data_services.acr_login_server
  agent_runtime_identity_id  = module.identity.agent_runtime_identity_id
  app_env_vars = merge(
    var.app_env_vars,
    {
      SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE = module.data_services.servicebus_namespace_fqdn
      AZURE_FOUNDRY_ENDPOINT                = module.foundry.foundry_endpoint
    }
  )
  app_secret_env_var_secret_ids    = local.app_secret_env_var_secret_ids
  container_image_tag              = var.container_image_tag
  container_cpu                    = var.agent_container_cpu
  container_memory                 = var.agent_container_memory
  min_replicas                     = var.agent_min_replicas
  max_replicas                     = var.agent_max_replicas
  agent_client_id                  = data.azurerm_user_assigned_identity.agent.client_id
  notification_runtime_identity_id = module.identity.notification_runtime_identity_id
  notification_client_id           = module.identity.notification_runtime_client_id
  notification_app_env_vars = merge(
    var.app_env_vars,
    {
      SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE = module.data_services.servicebus_namespace_fqdn
      NOTIFICATION_QUEUE_NAME               = module.data_services.notification_queue_name
      NOTIFICATION_MANUAL_QUEUE_NAME        = module.data_services.notification_manual_queue_name
      APP_CONFIGURATION_ENDPOINT            = module.data_services.app_configuration_endpoint
      WEBHOOK_SECRETS_VAULT_URL             = azurerm_key_vault.webhook_secrets.vault_uri
      APP_CONFIGURATION_LABEL               = var.environment
    }
  )
}

variable "jumpbox_ssh_public_key_secret_name" {
  type        = string
  description = "Key Vault secret name for jumpbox SSH public key."
  default     = ""
}