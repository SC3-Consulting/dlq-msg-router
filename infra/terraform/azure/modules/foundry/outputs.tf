output "foundry_account_id" {
  value = azurerm_cognitive_account.foundry.id
}

output "foundry_account_name" {
  value = azurerm_cognitive_account.foundry.name
}

output "foundry_endpoint" {
  value = "https://${azurerm_cognitive_account.foundry.name}.cognitiveservices.azure.com/"
}
