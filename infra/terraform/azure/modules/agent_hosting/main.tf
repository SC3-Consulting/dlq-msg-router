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
  name                         = "ca-dlq-msg-router-${var.suffix}"
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

  dynamic "secret" {
    for_each = var.app_secret_env_var_secret_ids
    content {
      name                = lower(replace(secret.key, "_", "-"))
      key_vault_secret_id = secret.value
      identity            = var.agent_runtime_identity_id
    }
  }

  template {
    min_replicas = var.min_replicas
    max_replicas = var.max_replicas

    container {
      name   = "router-agent"
      image  = "${var.acr_login_server}/router-agent:${var.container_image_tag}"
      cpu    = var.container_cpu
      memory = var.container_memory

      dynamic "env" {
        for_each = var.app_env_vars
        content {
          name  = env.key
          value = env.value
        }
      }

      dynamic "env" {
        for_each = var.app_secret_env_var_secret_ids
        content {
          name        = env.key
          secret_name = lower(replace(env.key, "_", "-"))
        }
      }

      # Forces the Python SDK to use the User-Assigned Managed Identity
      env {
        name  = "AZURE_CLIENT_ID"
        value = var.agent_client_id
      }
    }
  }

  tags = var.tags
}