#!/usr/bin/env bash

##########################
# 01-bootstrap.sh
# Bootstraps the remote Terraform state resources.
#
# Usage:
#   ./infra/scripts/01-bootstrap.sh -e dev -l australiaeast
##########################
set -euo pipefail

ENVIRONMENT="dev"
LOCATION="australiaeast"

while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--environment) ENVIRONMENT="$2"; shift 2 ;;
    -l|--location) LOCATION="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly BOOTSTRAP_DIR="${ROOT_DIR}/infra/terraform/azure/bootstrap"
readonly ENV_DIR="${ROOT_DIR}/infra/terraform/azure/environments/${ENVIRONMENT}"

mkdir -p "${ENV_DIR}"

echo "==> Verifying Azure login status..."
if ! az account show >/dev/null 2>&1; then
  echo "Azure CLI is not authenticated. Run 'az login' first."
  exit 1
fi

readonly RESOURCE_GROUP_NAME="rg-dlq-msg-router-${ENVIRONMENT}"

# Validate Resource Group exists before Terraform data block crashes
echo "==> Validating Resource Group existence..."
if ! az group exists --name "${RESOURCE_GROUP_NAME}" -o tsv | grep -q "true"; then
  echo "Error: Resource group '${RESOURCE_GROUP_NAME}' does not exist. Create it first." >&2
  exit 1
fi

# automated SSH key management
readonly SSH_KEY_PATH="${HOME}/.ssh/dlq_jumpbox_rsa"
if [[ ! -f "${SSH_KEY_PATH}" ]]; then
  echo "==> Generating Jumpbox SSH Key Pair..."
  mkdir -p "${HOME}/.ssh" && chmod 700 "${HOME}/.ssh"
  ssh-keygen -m PEM -t rsa -b 4096 -f "${SSH_KEY_PATH}" -N ""
fi
readonly SSH_PUB_KEY_CONTENT=$(cat "${SSH_KEY_PATH}.pub")

echo "==> Registering Azure resource providers..."
readonly PROVIDERS=("Microsoft.App" "Microsoft.KeyVault" "Microsoft.ServiceBus" "Microsoft.ContainerRegistry" "Microsoft.Storage")
for provider in "${PROVIDERS[@]}"; do
  az provider register --namespace "$provider" >/dev/null
  for attempt in {1..20}; do
    state=$(az provider show --namespace "$provider" --query registrationState -o tsv 2>/dev/null || true)
    if [[ "$state" == "Registered" ]]; then
      break
    fi
    echo "    Waiting for provider ${provider} registration... (${attempt}/20)"
    sleep 3
  done
  if [[ "$state" != "Registered" ]]; then
    echo "Error: Azure provider ${provider} failed to reach 'Registered' state." >&2
    exit 1
  fi
done

readonly STORAGE_ACCOUNT_PREFIX="sttfstate${ENVIRONMENT}"
readonly KEY_VAULT_PREFIX="kvtfstate"
readonly VAULT_TARGET_NAME="${KEY_VAULT_PREFIX}${ENVIRONMENT}"

# Check for soft-deleted vaults
if az keyvault list-deleted --query "[?name=='${VAULT_TARGET_NAME}'].name" -o tsv | grep -q "^${VAULT_TARGET_NAME}$"; then
  echo "==> Purging soft-deleted Key Vault: ${VAULT_TARGET_NAME}..."
  az keyvault purge --name "${VAULT_TARGET_NAME}" --location "${LOCATION}" || true
fi

echo "==> Applying bootstrap Terraform stack..."
cd "${BOOTSTRAP_DIR}"
rm -rf .terraform
terraform init -upgrade
terraform apply -auto-approve -input=false \
  -var="location=${LOCATION}" \
  -var="resource_group_name=${RESOURCE_GROUP_NAME}" \
  -var="storage_account_name_prefix=${STORAGE_ACCOUNT_PREFIX}" \
  -var="enable_bootstrap_key_vault=true" \
  -var="key_vault_name_prefix=${KEY_VAULT_PREFIX}"

readonly STATE_RG=$(terraform output -raw resource_group_name)
readonly STATE_SA=$(terraform output -raw storage_account_name)
readonly STATE_CONTAINER=$(terraform output -raw container_name)
readonly STATE_KEYVAULT=$(terraform output -raw key_vault_name)

echo "==> Injecting public key into Key Vault..."
az keyvault secret set --vault-name "${STATE_KEYVAULT}" --name "jumpbox-admin-ssh-public-key-${ENVIRONMENT}" --value "${SSH_PUB_KEY_CONTENT}" >/dev/null

echo "==> Generating backend.hcl and bootstrap vars..."
cat <<EOF > "${ENV_DIR}/backend.hcl"
resource_group_name  = "${STATE_RG}"
storage_account_name = "${STATE_SA}"
container_name       = "${STATE_CONTAINER}"
key                  = "platform/${ENVIRONMENT}.tfstate"
use_azuread_auth     = true
EOF

cat <<EOF > "${ENV_DIR}/bootstrap.generated.tfvars"
bootstrap_key_vault_name = "${STATE_KEYVAULT}"
bootstrap_key_vault_resource_group_name = "${STATE_RG}"
jumpbox_ssh_public_key_secret_name = "jumpbox-admin-ssh-public-key-${ENVIRONMENT}"
EOF

echo "==> Bootstrap complete."