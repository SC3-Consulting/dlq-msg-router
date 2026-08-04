variable "resource_group_name" { type = string }
variable "location" { type = string }
variable "suffix" { type = string }

variable "acr_name_override" {
  type        = string
  description = "Optional explicit ACR name override. Leave empty to use env-randomized ACR naming."
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