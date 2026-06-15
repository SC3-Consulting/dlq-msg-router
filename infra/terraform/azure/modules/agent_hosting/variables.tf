variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "suffix" { type = string }
variable "delegated_agent_subnet_id" { type = string }
variable "log_analytics_workspace_id" { type = string }
variable "acr_login_server" { type = string }
variable "agent_runtime_identity_id" { type = string }

variable "app_env_vars" {
  type        = map(string)
  description = "Dynamic environment variables loaded from .env"
  default     = {}
}

variable "tags" {
  type    = map(string)
  default = {}
}

variable "agent_client_id" {
  description = "The Client ID of the User Assigned Managed Identity"
  type        = string
}