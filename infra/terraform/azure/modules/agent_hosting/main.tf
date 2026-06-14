resource "azurerm_container_app_environment" "this" {
  name                               = "cae-${var.suffix}"
  location                           = var.location
  resource_group_name                = var.resource_group_name
  infrastructure_resource_group_name = "ME_cae-${var.suffix}_${var.resource_group_name}_${var.location}"
  log_analytics_workspace_id         = var.log_analytics_workspace_id
  infrastructure_subnet_id           = var.delegated_agent_subnet_id
  internal_load_balancer_enabled     = true
  tags                               = var.tags

  workload_profile {
    name                  = "Consumption"
    workload_profile_type = "Consumption"
  }
}

resource "azurerm_container_app" "dlq_agent" {
  name                         = "ca-viva-dlq-agent-${var.suffix}"
  resource_group_name          = var.resource_group_name
  container_app_environment_id = azurerm_container_app_environment.this.id
  revision_mode                = "Single"

  identity {
    type         = "UserAssigned"
    identity_ids = [var.agent_runtime_identity_id]
  }

  registry {
    server   = var.acr_login_server
    identity = var.agent_runtime_identity_id
  }

  template {
    min_replicas = 1
    max_replicas = 1

    container {
      name   = "viva-dlq-agent"
      image  = "${var.acr_login_server}/viva-dlq-agent:v1.0.0"
      cpu    = 0.5
      memory = "1Gi"

      dynamic "env" {
        for_each = var.app_env_vars
        content {
          name  = env.key
          value = env.value
        }
      }
    }
  }

  tags = var.tags
}