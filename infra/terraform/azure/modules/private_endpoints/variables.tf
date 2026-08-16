variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "private_endpoint_subnet_id" { type = string }
variable "private_dns_zone_ids" {
  type    = map(string)
  default = {}
}
variable "foundry_account_id" { type = string }
variable "foundry_account_name" { type = string }
variable "acr_id" { type = string }
variable "service_bus_id" { type = string }
variable "app_configuration_id" { type = string }
variable "webhook_key_vault_id" { type = string }