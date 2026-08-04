output "foundry_account_id" {
  value      = azurerm_cognitive_account.foundry.id
  depends_on = [time_sleep.wait_for_foundry_ready]
}

output "foundry_account_name" {
  value      = azurerm_cognitive_account.foundry.name
  depends_on = [time_sleep.wait_for_foundry_ready]
}

output "foundry_endpoint" {
  value      = "https://${azurerm_cognitive_account.foundry.name}.cognitiveservices.azure.com/"
  depends_on = [time_sleep.wait_for_foundry_ready]
}
