output "acr_login_server" {
  description = "The login server for the Azure Container Registry"
  value       = module.data_services.acr_login_server
}

output "servicebus_namespace_fqdn" {
  description = "The fully qualified domain name for the Service Bus namespace"
  value       = module.data_services.servicebus_namespace_fqdn
}

output "foundry_endpoint" {
  description = "The Azure AI Foundry endpoint for the configured model deployment"
  value       = module.foundry.foundry_endpoint
}

output "agent_identity_client_id" {
  description = "The User Assigned Managed Identity client ID used by the agent runtime"
  value       = module.identity.agent_runtime_client_id
}
