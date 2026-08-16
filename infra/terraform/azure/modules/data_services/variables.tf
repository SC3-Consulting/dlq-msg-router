variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "suffix" { type = string }

variable "acr_name_override" {
  type        = string
  description = "Optional explicit ACR name override. Leave empty to use env-randomised ACR naming."
  default     = ""
}

variable "acr_name_seed" {
  type        = string
  description = "Seed used in random name keepers to allow controlled ACR name rotation."
  default     = "v1"
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "servicebus_name_seed" {
  type        = string
  description = "Seed used in random name keepers to allow controlled Service Bus name rotation."
  default     = "v1"
}

variable "webhook_registry" {
  description = "Non-secret webhook metadata. Secret values remain in Key Vault."
  type = map(object({
    endpoint    = string
    secret_name = string
    enabled     = optional(bool, true)
    version     = optional(string, "v1")
  }))
  default = {}
}

variable "app_configuration_data_owner_principal_id" {
  description = "Object ID of the Terraform deployer granted data-plane access to write the initial registry key."
  type        = string
}

variable "app_configuration_management_principal_id" {
  description = "Object ID of the identity Terraform uses to read App Configuration store metadata and keys."
  type        = string
}