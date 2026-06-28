variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "suffix" { type = string }
variable "delegated_agent_subnet_id" { type = string }
variable "log_analytics_workspace_id" { type = string }
variable "acr_login_server" { type = string }
variable "agent_runtime_identity_id" { type = string }
variable "container_image_tag" { type = string }

variable "container_cpu" {
  description = "Container CPU cores for the DLQ agent container app."
  type        = number
  default     = 0.5
}

variable "container_memory" {
  description = "Container memory for the DLQ agent container app. Example: 1Gi"
  type        = string
  default     = "1Gi"
}

variable "min_replicas" {
  description = "Minimum replicas for the DLQ agent container app."
  type        = number
  default     = 1
}

variable "max_replicas" {
  description = "Maximum replicas for the DLQ agent container app."
  type        = number
  default     = 1
}

variable "app_env_vars" {
  type        = map(string)
  description = "Non-secret application environment variables from environment tfvars"
  default     = {}
}

variable "app_secret_env_var_secret_ids" {
  type        = map(string)
  description = "Map of environment variable name to Key Vault secret ID"
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