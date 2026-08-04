environment                  = "test"
location                     = "australiaeast"
resource_group_name          = "rg-dlq-msg-router-test"
vnet_cidr                    = "10.30.0.0/16"
private_endpoint_subnet_cidr = "10.30.1.0/24"
agent_subnet_cidr            = "10.30.2.0/24"
container_apps_subnet_cidr   = "10.30.5.0/24"
jumpbox_subnet_cidr          = "10.30.3.0/24"
azure_bastion_subnet_cidr    = "10.30.4.0/24"
jumpbox_vm_size              = "Standard_B2s"
container_image_tag          = "v1.0.0-test"
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
  AZURE_FOUNDRY_ENDPOINT        = "https://foundry-dlq-msg-router-test.cognitiveservices.azure.com/openai/deployments/gpt-4o-mini"
  AZURE_FOUNDRY_DEPLOYMENT_NAME = "gpt-4o-mini"
  AZURE_FOUNDRY_MAX_TOKENS      = "1200"
  AZURE_FOUNDRY_EMPTY_RESPONSE_MAX_TOKENS = "2400"
  AI_RETRY_MAX_ATTEMPTS         = "5"
  AI_BACKOFF_BASE_SECONDS       = "1.5"
  AI_BACKOFF_MAX_SECONDS        = "20"
  RULES_FILE_PATH               = "data/rules.json"
  AGENT_CYCLE_SLEEP_SECONDS     = "60"
}

app_secret_env_var_secret_names = {}

tags = {
  environment = "test"
  service     = "dlq-msg-router-agent"
  managed_by  = "terraform"
}
