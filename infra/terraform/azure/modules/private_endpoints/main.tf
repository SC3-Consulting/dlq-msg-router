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

  foundry_private_ips = compact([
    azurerm_private_endpoint.this["foundry_account"].private_service_connection[0].private_ip_address
  ])
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

resource "azurerm_private_dns_a_record" "foundry_cognitiveservices" {
  name                = var.foundry_account_name
  zone_name           = "privatelink.cognitiveservices.azure.com"
  resource_group_name = var.resource_group_name
  ttl                 = 300
  records             = local.foundry_private_ips
}

resource "azurerm_private_dns_a_record" "foundry_openai" {
  name                = var.foundry_account_name
  zone_name           = "privatelink.openai.azure.com"
  resource_group_name = var.resource_group_name
  ttl                 = 300
  records             = local.foundry_private_ips
}