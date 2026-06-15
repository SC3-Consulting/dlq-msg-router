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
  source              = "./modules/data_services"
  resource_group_name = module.foundation.resource_group_name
  location            = var.location
  suffix              = var.environment
}

module "foundry" {
  source                    = "./modules/foundry"
  resource_group_name       = module.foundation.resource_group_name
  location                  = var.location
  suffix                    = var.environment
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
  acr_id                     = module.data_services.acr_id
  service_bus_id             = module.data_services.service_bus_id
}

module "identity" {
  source              = "./modules/identity"
  resource_group_name = module.foundation.resource_group_name
  location            = var.location
  suffix              = var.environment
  scope_ids = {
    foundry       = module.foundry.foundry_account_id
    acr           = module.data_services.acr_id
    service_bus   = module.data_services.service_bus_id
    log_analytics = module.observability.log_analytics_workspace_id
  }
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

module "agent_hosting" {
  source                     = "./modules/agent_hosting"
  resource_group_name        = module.foundation.resource_group_name
  location                   = var.location
  suffix                     = var.environment
  delegated_agent_subnet_id  = module.network.container_apps_subnet_id
  log_analytics_workspace_id = module.observability.log_analytics_workspace_id
  acr_login_server           = module.data_services.acr_login_server
  agent_runtime_identity_id  = module.identity.agent_runtime_identity_id
  app_env_vars               = var.app_env_vars
  agent_client_id            = data.azurerm_user_assigned_identity.agent.client_id
}

variable "jumpbox_ssh_public_key_secret_name" {
  type        = string
  description = "Key Vault secret name for jumpbox SSH public key."
  default     = ""
}