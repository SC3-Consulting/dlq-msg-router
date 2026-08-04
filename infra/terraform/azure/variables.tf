variable "environment" {
  type        = string
  description = "Environment name (dev/test/prod)."
}

variable "location" {
  type        = string
  description = "Azure region for all resources."
}

variable "resource_group_name" {
  type        = string
  description = "Primary resource group name."
}

variable "vnet_cidr" {
  type        = string
  description = "VNet CIDR block (/16)."
}

variable "private_endpoint_subnet_cidr" {
  type        = string
  description = "Private endpoint subnet CIDR block (/24)."
}

variable "agent_subnet_cidr" {
  type        = string
  description = "Delegated agent subnet CIDR block (/24)."
}

variable "container_apps_subnet_cidr" {
  type        = string
  description = "Dedicated Container Apps managed environment subnet CIDR block (/24)."
}

variable "jumpbox_subnet_cidr" {
  type        = string
  description = "Jumpbox subnet CIDR block."
}

variable "azure_bastion_subnet_cidr" {
  type        = string
  description = "Azure Bastion subnet CIDR block."
}

variable "jumpbox_vm_size" {
  type        = string
  description = "VM size for the jumpbox host."
}

variable "query_model" {
  type = object({
    name     = string
    version  = string
    capacity = optional(number, 1)
  })
  description = "Model deployment configuration for Azure Foundry query workloads."
  default = {
    name     = "gpt-5-mini"
    version  = "2025-08-07"
    capacity = 1
  }
}

variable "jumpbox_admin_ssh_public_key" {
  type        = string
  description = "SSH public key for jumpbox admin access."
  default     = ""
}

variable "bootstrap_key_vault_name" {
  type        = string
  description = "Bootstrap Key Vault name to read the SSH public key."
  default     = ""
}

variable "bootstrap_key_vault_resource_group_name" {
  type        = string
  description = "Bootstrap Key Vault resource group name."
  default     = ""
}

variable "app_env_vars" {
  type        = map(string)
  description = "Non-secret app environment variables provided via environment tfvars"
  default     = {}
}

variable "app_secret_env_var_secret_names" {
  type        = map(string)
  description = "Map of environment variable name to Key Vault secret name"
  default     = {}
}

variable "container_image_tag" {
  type        = string
  description = "Immutable image tag for the router-agent container deployment."
}

variable "agent_container_cpu" {
  type        = number
  description = "CPU cores for the router agent container app instance."
  default     = 0.5
}

variable "agent_container_memory" {
  type        = string
  description = "Memory size for the router agent container app instance."
  default     = "1Gi"
}

variable "agent_min_replicas" {
  type        = number
  description = "Minimum number of container app replicas for the router agent."
  default     = 1
}

variable "agent_max_replicas" {
  type        = number
  description = "Maximum number of container app replicas for the router agent."
  default     = 1
}

variable "tags" {
  type    = map(string)
  default = {}
}