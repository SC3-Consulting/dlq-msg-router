#!/usr/bin/env bash

###############################################################################
# 02b-generate-local-env.sh
#
# SYNOPSIS
#   Generates the repository root .env file for the local Docker observability
#   and simulator stack.
#
# DESCRIPTION
#   Extracts runtime values from Terraform state and the authenticated Azure
#   CLI session, then writes a complete .env file to the repository root.
#   The script is intentionally strict so local stack automation fails fast
#   instead of relying on manual copy-paste of Azure IDs into .env files.
#
# PARAMETERS
#   -e, --environment   Target deployment environment (dev, test, prod). Default: dev
###############################################################################

set -euo pipefail

ENVIRONMENT="dev"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--environment)
      ENVIRONMENT="$2"
      shift 2
      ;;
    *)
      echo "[-] Unknown option: $1" >&2
      exit 1
      ;;
  esac
done

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly TF_DIR="${ROOT_DIR}/infra/terraform/azure"
readonly BACKEND_FILE="${TF_DIR}/environments/${ENVIRONMENT}/backend.hcl"
readonly BOOTSTRAP_VARS_FILE="${TF_DIR}/environments/${ENVIRONMENT}/bootstrap.generated.tfvars"
readonly ENV_FILE="${ROOT_DIR}/.env"

dotenv_escape() {
  local value="${1-}"
  value="${value//\\/\\\\}"
  value="${value//\"/\\\"}"
  printf '%s' "${value}"
}

require_terraform_output_raw() {
  local output_name="$1"
  local result_var_name="$2"
  local output_value

  if ! output_value="$(terraform output -raw "${output_name}" 2>/dev/null)"; then
    echo "[-] Error: Unable to read Terraform output '${output_name}'. Ensure Phase 2 has been applied and state is reachable." >&2
    exit 1
  fi

  if [[ -z "${output_value}" ]]; then
    echo "[-] Error: Terraform output '${output_name}' is empty." >&2
    exit 1
  fi

  printf -v "${result_var_name}" '%s' "${output_value}"
}

require_az_tsv() {
  local query="$1"
  local result_var_name="$2"
  local value

  if ! value="$(az account show --query "${query}" -o tsv 2>/dev/null)"; then
    echo "[-] Error: Unable to query Azure CLI account field '${query}'." >&2
    exit 1
  fi

  if [[ -z "${value}" ]]; then
    echo "[-] Error: Azure CLI account field '${query}' is empty." >&2
    exit 1
  fi

  printf -v "${result_var_name}" '%s' "${value}"
}

if ! command -v terraform >/dev/null 2>&1; then
  echo "[-] Error: Terraform CLI is not installed or not in PATH." >&2
  exit 1
fi

if ! command -v az >/dev/null 2>&1; then
  echo "[-] Error: Azure CLI is not installed or not in PATH." >&2
  exit 1
fi

if ! az account show >/dev/null 2>&1; then
  echo "[-] Error: Azure CLI is not authenticated. Run 'az login' first." >&2
  exit 1
fi

if [[ ! -d "${TF_DIR}/.terraform" ]]; then
  echo "[-] Error: Terraform workspace is not initialised. Run Phase 1 and Phase 2 first." >&2
  exit 1
fi

for f in "${BACKEND_FILE}" "${BOOTSTRAP_VARS_FILE}"; do
  if [[ ! -f "${f}" ]]; then
    echo "[-] Error: Required Terraform runtime file missing at ${f}. Run Phase 1 and Phase 2 first." >&2
    exit 1
  fi
done

readonly RG_NAME="rg-dlq-msg-router-${ENVIRONMENT}"

echo "==> Extracting Terraform outputs for the local stack..."
cd "${TF_DIR}"
SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE=""
AZURE_CLIENT_ID=""
AZURE_FOUNDRY_ENDPOINT=""
AZURE_TENANT_ID=""
AZURE_SUBSCRIPTION_ID=""

require_terraform_output_raw servicebus_namespace_fqdn SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE
require_terraform_output_raw agent_identity_client_id AZURE_CLIENT_ID
require_terraform_output_raw foundry_endpoint AZURE_FOUNDRY_ENDPOINT
require_az_tsv tenantId AZURE_TENANT_ID
require_az_tsv id AZURE_SUBSCRIPTION_ID

readonly SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE
readonly AZURE_CLIENT_ID
readonly AZURE_FOUNDRY_ENDPOINT
readonly AZURE_FOUNDRY_DEPLOYMENT_NAME="${AZURE_FOUNDRY_ENDPOINT##*/}"
readonly AZURE_TENANT_ID
readonly AZURE_SUBSCRIPTION_ID

echo "==> Resolving Log Analytics workspace ID from resource group ${RG_NAME}..."
if ! LOG_ANALYTICS_WORKSPACE_COUNT="$(az monitor log-analytics workspace list \
  --resource-group "${RG_NAME}" \
  --query "length(@)" \
  -o tsv 2>/dev/null)"; then
  echo "[-] Error: Failed to query Log Analytics workspaces in resource group ${RG_NAME}." >&2
  exit 1
fi
readonly LOG_ANALYTICS_WORKSPACE_COUNT

if [[ "${LOG_ANALYTICS_WORKSPACE_COUNT}" != "1" ]]; then
  echo "[-] Error: Expected exactly 1 Log Analytics workspace in ${RG_NAME}, found ${LOG_ANALYTICS_WORKSPACE_COUNT}." >&2
  echo "[-] Resolve workspace count ambiguity before generating the local .env file." >&2
  exit 1
fi

if ! LOG_ANALYTICS_WORKSPACE_ID="$(az monitor log-analytics workspace list \
  --resource-group "${RG_NAME}" \
  --query "[0].id" \
  -o tsv 2>/dev/null)"; then
  echo "[-] Error: Failed to resolve Log Analytics workspace ID in resource group ${RG_NAME}." >&2
  exit 1
fi
readonly LOG_ANALYTICS_WORKSPACE_ID

if [[ -z "${LOG_ANALYTICS_WORKSPACE_ID}" ]]; then
  echo "[-] Error: Unable to resolve Log Analytics workspace ID in resource group ${RG_NAME}." >&2
  exit 1
fi

readonly TMP_ENV_FILE="$(mktemp "${ROOT_DIR}/.env.XXXXXX")"

cat > "${TMP_ENV_FILE}" <<EOF
# Azure Service Bus Configuration
SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE="$(dotenv_escape "${SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE}")"

# Azure Identity and Observability Wiring
AZURE_TENANT_ID="$(dotenv_escape "${AZURE_TENANT_ID}")"
AZURE_CLIENT_ID="$(dotenv_escape "${AZURE_CLIENT_ID}")"
AZURE_SUBSCRIPTION_ID="$(dotenv_escape "${AZURE_SUBSCRIPTION_ID}")"
LOG_ANALYTICS_WORKSPACE_ID="$(dotenv_escape "${LOG_ANALYTICS_WORKSPACE_ID}")"

# Toggle for RBAC-secured dynamic discovery of queues
ENABLE_DYNAMIC_DISCOVERY="True"
EXCLUDED_QUEUES="system-queue,archive-queue"

# Fallback JSON Array if Dynamic Discovery is disabled or hits an RBAC 403 error
ASB_SOURCES_FILE="data/asb_sources.json"
PARKING_LOT_QUEUE_NAME="parking-lot-queue"

# Optional Azure Service Bus emulator controls (opt-in)
EMULATOR_HTTP_PORT=5300
ACCEPT_EULA="N"
MSSQL_SA_PASSWORD=""
ENABLE_DEPENDENCY_WAIT="True"
DEPENDENCY_WAIT_TIMEOUT_SECONDS=120

# Maximum number of concurrent queues processed at any given second
MAX_CONCURRENT_QUEUES=5
ASB_MAX_MESSAGE_COUNT=10
ASB_MAX_WAIT_TIME=5
PREFETCH_COUNT=20

# Action Execution Feature Flags
ENABLE_NEW_MESSAGE_ID_ON_RETRY="True"

# Cache & Threshold Configuration
IDEMPOTENCY_TTL_SECONDS=86400
CLASSIFICATION_TTL_SECONDS=600
MAX_RESUBMIT_COUNT=3
DUPLICATE_NOISE_THRESHOLD=10

# Storage Paths
TELEMETRY_CSV_PATH="reports/telemetry_dashboard.csv"
IDEMPOTENCY_DB_PATH="data/idempotency.db"

# AI Client Configuration
OLLAMA_MODEL="qwen2.5:0.5b"
OLLAMA_ENDPOINT="http://localhost:11434/api/generate"
OLLAMA_TEMPERATURE=0.1
OLLAMA_TIMEOUT=240
OLLAMA_NUM_CTX=4096

# File Paths
RULES_FILE_PATH="data/rules.json"

# Options: OLLAMA, AZURE_FOUNDRY
AI_PROVIDER="AZURE_FOUNDRY"

# Azure Foundry Specifics (Required if AI_PROVIDER="AZURE_FOUNDRY")
AZURE_FOUNDRY_ENDPOINT="$(dotenv_escape "${AZURE_FOUNDRY_ENDPOINT}")"
AZURE_FOUNDRY_DEPLOYMENT_NAME="$(dotenv_escape "${AZURE_FOUNDRY_DEPLOYMENT_NAME}")"
AZURE_FOUNDRY_TEMPERATURE=1
AZURE_FOUNDRY_MAX_TOKENS=1200
AZURE_FOUNDRY_EMPTY_RESPONSE_MAX_TOKENS=2400

# Time to sleep (in seconds) after completing a full sweep of all queues
# Prevents aggressive AMQP link churn if all DLQs are empty.
AGENT_CYCLE_SLEEP_SECONDS=60
EOF

chmod 600 "${TMP_ENV_FILE}"
mv "${TMP_ENV_FILE}" "${ENV_FILE}"

echo "[+] Wrote local environment file to ${ENV_FILE}."