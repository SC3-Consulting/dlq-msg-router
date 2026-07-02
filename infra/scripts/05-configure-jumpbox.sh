#!/usr/bin/env bash

###############################################################################
# 05-configure-jumpbox.sh
#
# SYNOPSIS
#   Automates the provisioning of the Ubuntu Jumpbox via Azure Bastion.
#
# DESCRIPTION
#   Extracts runtime endpoints from Terraform state, connects to the Jumpbox
#   via a secure Bastion SSH tunnel, installs system dependencies, clones the
#   active repository, and dynamically generates the simulator .env file.
#
# PARAMETERS
#   -e, --environment   Target deployment environment (dev, test, prod). Default: dev
###############################################################################

set -euo pipefail

ENVIRONMENT="dev"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--environment) ENVIRONMENT="$2"; shift 2 ;;
    *) echo "[-] Unknown execution flag: $1" >&2; exit 1 ;;
  esac
done

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly TF_DIR="${ROOT_DIR}/infra/terraform/azure"
readonly SSH_KEY_PATH="${HOME}/.ssh/dlq_jumpbox_rsa"

# Pre-flight validation
if [[ ! -f "${SSH_KEY_PATH}" ]]; then
  echo "[-] Error: Jumpbox private key missing at ${SSH_KEY_PATH}. Run Phase 1." >&2
  exit 1
fi

if [[ ! -d "${TF_DIR}/.terraform" ]]; then
  echo "[-] Error: Terraform workspace uninitialised. Run Phase 2." >&2
  exit 1
fi

echo "==> Scraping dynamic infrastructure endpoints from Terraform state..."
cd "${TF_DIR}"
readonly SB_FQDN=$(terraform output -raw servicebus_namespace_fqdn)
readonly FOUNDRY_EP=$(terraform output -raw foundry_endpoint)
readonly CLIENT_ID=$(terraform output -raw agent_identity_client_id)

echo "==> Resolving active Git repository and branch..."
cd "${ROOT_DIR}"
readonly REPO_URL=$(git config --get remote.origin.url || true)
if [[ -z "${REPO_URL}" ]]; then
  echo "[-] Error: Remote origin URL not configured. Set up a valid git remote before running this script." >&2
  exit 1
fi
readonly REPO_NAME=$(basename -s .git "${REPO_URL}")
readonly CURRENT_BRANCH=$(git branch --show-current || true)

if [[ -z "${CURRENT_BRANCH}" ]]; then
  echo "[-] Error: Unable to determine current git branch. Ensure you are on a branch, not a detached HEAD." >&2
  exit 1
fi

if [[ "${CURRENT_BRANCH}" != "bash-migration-test" ]]; then
  echo "[-] Error: This script must be run from the 'bash-migration-test' branch. Current branch is '${CURRENT_BRANCH}'." >&2
  exit 1
fi

readonly RG_NAME="rg-viva-dlq-${ENVIRONMENT}"
readonly VM_NAME="vm-jumpbox-${ENVIRONMENT}"

echo "==> Initiating secure Bastion SSH payload delivery to ${VM_NAME}..."

# We pipe a self-contained setup script directly into the Bastion SSH session.
# 'EOF' is deliberately unquoted so local Bash variables are evaluated before injection.
az network bastion ssh \
  --name "bas-${ENVIRONMENT}" \
  --resource-group "${RG_NAME}" \
  --target-resource-id $(az vm show --resource-group "${RG_NAME}" --name "${VM_NAME}" --query id -o tsv) \
  --auth-type "ssh-key" \
  --username "azureuser" \
  --ssh-key "${SSH_KEY_PATH}" \
  --command "bash -s" << EOF
set -euo pipefail

echo "    [+] (Remote) Installing Azure CLI..."
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash >/dev/null 2>&1

echo "    [+] (Remote) Updating APT packages and installing Python/Docker toolchains..."
sudo apt-get update >/dev/null 2>&1
sudo apt-get install -y docker.io python3-pip python3-venv >/dev/null 2>&1
sudo chmod 666 /var/run/docker.sock || true

echo "    [+] (Remote) Synchronising codebase from ${REPO_URL} (Branch: ${CURRENT_BRANCH})..."
if [[ ! -d "${REPO_NAME}" ]]; then
  git clone "${REPO_URL}" >/dev/null 2>&1
fi

cd "${REPO_NAME}"
git fetch --all >/dev/null 2>&1
if ! git ls-remote --heads origin "${CURRENT_BRANCH}" | grep -q "refs/heads/${CURRENT_BRANCH}"; then
  echo "[-] Error: Remote branch ${CURRENT_BRANCH} does not exist on origin. Push it first." >&2
  exit 1
fi
git checkout "${CURRENT_BRANCH}" >/dev/null 2>&1
git pull origin "${CURRENT_BRANCH}" >/dev/null 2>&1

echo "    [+] (Remote) Constructing Python virtual environment..."
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt >/dev/null 2>&1

echo "    [+] (Remote) Generating dynamic simulator .env configuration..."
cat << INNER_EOF > .env
# Azure Service Bus Configuration
SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE="${SB_FQDN}"
AZURE_CLIENT_ID="${CLIENT_ID}"

# Toggle for RBAC-secured dynamic discovery of queues
ENABLE_DYNAMIC_DISCOVERY="True"
EXCLUDED_QUEUES="parking-lot-queue,system-queue,archive-queue"

# Fallback JSON Array if Dynamic Discovery is disabled
ASB_SOURCES_FILE="data/asb_sources.json"
PARKING_LOT_QUEUE_NAME="parking-lot-queue"

MAX_CONCURRENT_QUEUES=5
ASB_MAX_MESSAGE_COUNT=10
ASB_MAX_WAIT_TIME=5
PREFETCH_COUNT=20

ENABLE_NEW_MESSAGE_ID_ON_RETRY="True"

IDEMPOTENCY_TTL_SECONDS=86400
CLASSIFICATION_TTL_SECONDS=600
MAX_RESUBMIT_COUNT=3
DUPLICATE_NOISE_THRESHOLD=10

TELEMETRY_CSV_PATH="reports/telemetry_dashboard.csv"
IDEMPOTENCY_DB_PATH="data/idempotency.db"

OLLAMA_MODEL="qwen2.5:0.5b"
OLLAMA_ENDPOINT="http://localhost:11434/api/generate"
OLLAMA_TEMPERATURE=0.1
OLLAMA_NUM_CTX=4096
OLLAMA_TIMEOUT=240

RULES_FILE_PATH="data/rules.json"

AI_PROVIDER="AZURE_FOUNDRY"
AZURE_FOUNDRY_ENDPOINT="${FOUNDRY_EP}"
AZURE_FOUNDRY_DEPLOYMENT_NAME="gpt-4o-mini"
AZURE_FOUNDRY_TEMPERATURE=0.1
AGENT_CYCLE_SLEEP_SECONDS=60
AZURE_FOUNDRY_MAX_TOKENS=300
INNER_EOF

echo "    [+] (Remote) Jumpbox environment fully provisioned and ready for live-fire simulation."
EOF

echo "[+] Phase 3 Bastion Configuration Complete. Simulator environment is armed."