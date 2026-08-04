locals {
  endpoints = {
    foundry_account = {
      resource_id      = var.foundry_account_id
      subresource_name = "account"
      zone_names = [
        "privatelink.cognitiveservices.azure.com",
        "privatelink.openai.azure.com"
      ]
    }
    acr = {
      resource_id      = var.acr_id
      subresource_name = "registry"
      zone_names = [
        "privatelink.azurecr.io"
      ]
    }
    servicebus = {
      resource_id      = var.service_bus_id
      subresource_name = "namespace"
      zone_names = [
        "privatelink.servicebus.windows.net"
      ]
    }
  }

}

resource "azurerm_private_endpoint" "this" {
  for_each            = local.endpoints
  name                = "pe-${replace(each.key, "_", "-")}"
  location            = var.location
  resource_group_name = var.resource_group_name
  subnet_id           = var.private_endpoint_subnet_id

  private_service_connection {
    name                           = "psc-${replace(each.key, "_", "-")}"
    private_connection_resource_id = each.value.resource_id
    subresource_names              = [each.value.subresource_name]
    is_manual_connection           = false
  }

  private_dns_zone_group {
    name                 = "zg-${replace(each.key, "_", "-")}"
    private_dns_zone_ids = [for zone_name in each.value.zone_names : var.private_dns_zone_ids[zone_name]]
  }
}
