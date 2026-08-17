resource "azurerm_public_ip" "bastion" {
  name                = "pip-bastion-${var.suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  allocation_method   = "Static"
  sku                 = "Standard"
  tags                = var.tags
}

resource "azurerm_bastion_host" "this" {
  name                = "bas-${var.suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name
  sku                 = "Standard"
  tunneling_enabled   = true
  tags                = var.tags

  ip_configuration {
    name                 = "configuration"
    subnet_id            = var.azure_bastion_subnet_id
    public_ip_address_id = azurerm_public_ip.bastion.id
  }
}

resource "azurerm_network_interface" "jumpbox" {
  name                = "nic-jumpbox-${var.suffix}"
  location            = var.location
  resource_group_name = var.resource_group_name

  ip_configuration {
    name                          = "internal"
    subnet_id                     = var.jumpbox_subnet_id
    private_ip_address_allocation = "Dynamic"
  }

  tags = var.tags
}

resource "azurerm_linux_virtual_machine" "jumpbox" {
  name                = "vm-jumpbox-${var.suffix}"
  resource_group_name = var.resource_group_name
  location            = var.location
  size                = var.jumpbox_vm_size
  admin_username      = "azureuser"
  network_interface_ids = [
    azurerm_network_interface.jumpbox.id
  ]
  disable_password_authentication = true

  admin_ssh_key {
    username   = "azureuser"
    public_key = var.jumpbox_admin_ssh_public_key
  }

  os_disk {
    caching              = "ReadWrite"
    storage_account_type = "Standard_LRS"
  }

  source_image_reference {
    publisher = "Canonical"
    offer     = "0001-com-ubuntu-server-jammy"
    sku       = "22_04-lts-gen2"
    version   = "latest"
  }

  # Attach the agent runtime user-assigned MI so DefaultAzureCredential
  # resolves without any explicit client ID or credential on the VM.
  identity {
    type         = "UserAssigned"
    identity_ids = [var.agent_runtime_identity_id]
  }

  tags = var.tags
}

resource "azurerm_monitor_data_collection_rule" "jumpbox_vm_insights" {
  name                = "dcr-jumpbox-vminsights-${var.suffix}"
  resource_group_name = var.resource_group_name
  location            = var.location

  data_sources {
    performance_counter {
      name                          = "perfCounterDataSource"
      sampling_frequency_in_seconds = 60
      streams                      = ["Microsoft-InsightsMetrics"]
      counter_specifiers = [
        "\\Processor Information(_Total)\\% Processor Time",
        "\\Processor Information(_Total)\\% Privileged Time",
        "\\Memory\\Available MBytes",
        "\\LogicalDisk(_Total)\\% Free Space",
        "\\PhysicalDisk(_Total)\\Disk Transfers/sec",
        "\\Network Interface(*)\\Bytes Total/sec"
      ]
    }

    extension {
      name     = "DependencyAgentDataSource"
      extension_name = "DependencyAgentLinux"
      streams = ["Microsoft-InsightsMetrics"]
    }
  }

  destinations {
    log_analytics {
      name                  = "logAnalyticsDestination"
      workspace_resource_id = var.log_analytics_workspace_id
    }
  }

  data_flow {
    streams      = ["Microsoft-InsightsMetrics"]
    destinations = ["logAnalyticsDestination"]
  }
}

resource "azurerm_monitor_data_collection_rule_association" "jumpbox_vm_insights" {
  name                    = "assoc-jumpbox-vminsights-${var.suffix}"
  target_resource_id      = azurerm_linux_virtual_machine.jumpbox.id
  data_collection_rule_id = azurerm_monitor_data_collection_rule.jumpbox_vm_insights.id
}

resource "azurerm_virtual_machine_extension" "jumpbox_azure_monitor_agent" {
  name                       = "AzureMonitorLinuxAgent"
  virtual_machine_id         = azurerm_linux_virtual_machine.jumpbox.id
  publisher                  = "Microsoft.Azure.Monitor"
  type                      = "AzureMonitorLinuxAgent"
  type_handler_version      = "1.33"
  auto_upgrade_minor_version = true

  settings = jsonencode({
    enableAgent = true
  })

  depends_on = [azurerm_monitor_data_collection_rule_association.jumpbox_vm_insights]
}
