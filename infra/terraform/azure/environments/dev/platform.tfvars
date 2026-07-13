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
container_image_tag          = "v1.0.2-dev-aifix-endpoint"
agent_container_cpu          = 0.5
agent_container_memory       = "1Gi"
agent_min_replicas           = 1
agent_max_replicas           = 1

app_env_vars = {
  ENABLE_DYNAMIC_DISCOVERY      = "True"
  EXCLUDED_QUEUES               = "parking-lot-queue"
  PARKING_LOT_QUEUE_NAME        = "parking-lot-queue"
  MAX_CONCURRENT_QUEUES         = "5"
  ASB_MAX_MESSAGE_COUNT         = "10"
  ASB_MAX_WAIT_TIME             = "5"
  PREFETCH_COUNT                = "20"
  ENABLE_HEALTH_ENDPOINTS       = "true"
  HEALTHCHECK_HOST              = "0.0.0.0"
  HEALTHCHECK_PORT              = "8080"
  SHUTDOWN_TIMEOUT_SECONDS      = "30"
  AI_PROVIDER                   = "AZURE_FOUNDRY"
  AZURE_FOUNDRY_ENDPOINT        = "https://foundry-dlq-msg-router-dev.cognitiveservices.azure.com/openai/deployments/gpt-5-mini"
  AZURE_FOUNDRY_DEPLOYMENT_NAME = "gpt-5-mini"
  AZURE_FOUNDRY_MAX_TOKENS      = "300"
  RULES_FILE_PATH               = "data/rules.json"
  AGENT_CYCLE_SLEEP_SECONDS     = "60"
}

query_model = {
  name     = "gpt-5-mini"
  version  = "2025-08-07"
  capacity = 1
}

# Map sensitive runtime settings to Key Vault secret names.
# Example:
# app_secret_env_var_secret_names = {
#   SERVICE_BUS_CONNECTION_STRING = "service-bus-connection-string-dev"
# }
app_secret_env_var_secret_names = {}

tags = {
  environment = "dev"
  service     = "dlq-msg-router-agent"
  managed_by  = "terraform"
}
