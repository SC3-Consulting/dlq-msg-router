variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "suffix" { type = string }
variable "delegated_agent_subnet_id" { type = string }

variable "query_model" {
  type = object({
    name     = string
    version  = string
    capacity = optional(number, 1)
  })
  default = {
    name     = "gpt-4o-mini"
    version  = "2024-07-18"
    capacity = 1
  }
}

variable "enable_model_deployments" {
  type    = bool
  default = false
}