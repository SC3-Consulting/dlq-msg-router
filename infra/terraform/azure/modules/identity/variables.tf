variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "suffix" { type = string }

variable "agent_runtime_identity_name_override" {
  type        = string
  description = "Optional explicit runtime UAMI name override. Leave empty to use id-agent-runtime-<suffix>."
  default     = ""
}

variable "scope_ids" { 
  type = map(string) 
}

variable "tags" {
  type    = map(string)
  default = {}
}