environment                  = "dev"
location                     = "australiaeast"
resource_group_name          = "rg-dlq-msg-router-dev"
vnet_cidr                    = "10.20.0.0/16"
private_endpoint_subnet_cidr = "10.20.1.0/24"
agent_subnet_cidr            = "10.20.2.0/24"
container_apps_subnet_cidr   = "10.20.5.0/24"
jumpbox_subnet_cidr          = "10.20.3.0/24"
azure_bastion_subnet_cidr    = "10.20.4.0/24"
jumpbox_vm_size              = "Standard_D2s_v3"
container_image_tag          = "v1.0.3-dev-aifix-model-extras-20260714013003"
agent_container_cpu          = 0.5
agent_container_memory       = "1Gi"
agent_min_replicas           = 1
agent_max_replicas           = 1

app_env_vars = {
  ENABLE_DYNAMIC_DISCOVERY                = "True"
  EXCLUDED_QUEUES                         = "parking-lot-queue,notification-queue,notification-manual-queue"
  PARKING_LOT_QUEUE_NAME                  = "parking-lot-queue"
  NOTIFICATION_ENABLED                    = "true"
  NOTIFICATION_QUEUE_NAME                 = "notification-queue"
  NOTIFICATION_MANUAL_QUEUE_NAME          = "notification-manual-queue"
  MAX_CONCURRENT_QUEUES                   = "5"
  ASB_MAX_MESSAGE_COUNT                   = "10"
  ASB_MAX_WAIT_TIME                       = "5"
  PREFETCH_COUNT                          = "20"
  ENABLE_HEALTH_ENDPOINTS                 = "true"
  HEALTHCHECK_HOST                        = "0.0.0.0"
  HEALTHCHECK_PORT                        = "8080"
  SHUTDOWN_TIMEOUT_SECONDS                = "30"
  AI_PROVIDER                             = "AZURE_FOUNDRY"
  AZURE_FOUNDRY_ENDPOINT                  = "https://foundry-dlq-msg-router-dev.cognitiveservices.azure.com/openai/deployments/gpt-5.4"
  AZURE_FOUNDRY_DEPLOYMENT_NAME           = "gpt-5.4"
  AZURE_FOUNDRY_MAX_TOKENS                = "1200"
  AZURE_FOUNDRY_EMPTY_RESPONSE_MAX_TOKENS = "2400"
  AI_RETRY_MAX_ATTEMPTS                   = "5"
  AI_BACKOFF_BASE_SECONDS                 = "1.5"
  AI_BACKOFF_MAX_SECONDS                  = "20"
  RULES_FILE_PATH                         = "data/rules.json"
  AGENT_CYCLE_SLEEP_SECONDS               = "60"
}

query_model = {
  name     = "gpt-5.4"
  version  = "2026-03-05"
  capacity = 1
}

# Map sensitive runtime settings to Key Vault secret names.
# Example:
# app_secret_env_var_secret_names = {
#   SERVICE_BUS_CONNECTION_STRING = "service-bus-connection-string-dev"
# }
app_secret_env_var_secret_names = {}

# Non-secret registry metadata. Create each referenced secret in the runtime Key Vault separately.
webhook_registry = {
  Client_A = {
    endpoint    = "https://client.example/notifications"
    secret_name = "client-a-hmac"
    enabled     = true
    version     = "v1"
  }
}

# Stable object ID of the original Terraform deployment user. Keep this stable when
# running the App Configuration key apply from the jumpbox managed identity.
app_configuration_deployer_object_id = "601e769e-2e48-431f-bff3-fb9f69ace993"

tags = {
  environment = "dev"
  service     = "dlq-msg-router-agent"
  managed_by  = "terraform"
}
