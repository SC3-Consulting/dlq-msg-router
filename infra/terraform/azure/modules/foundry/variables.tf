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
    name     = "gpt-5-mini"
    version  = "2025-08-07"
    capacity = 1
  }
}

variable "enable_model_deployments" {
  type    = bool
  default = false
}

variable "foundry_name_seed" {
  type        = string
  description = "Seed used in random name keepers to allow controlled Foundry name rotation."
  default     = "v2"
}

variable "foundry_ready_wait_duration" {
  type        = string
  description = "Delay after Cognitive Account creation before dependent resources are provisioned."
  default     = "90s"
}