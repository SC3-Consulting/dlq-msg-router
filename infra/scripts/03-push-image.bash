#!/usr/bin/env bash

##########################
# 03-push-image.sh
# Builds and pushes the Router Agent image to ACR
#
# Usage:
#   ./infra/scripts/03-push-image.sh -e dev
##########################
set -euo pipefail

ENVIRONMENT="dev"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--environment) ENVIRONMENT="$2"; shift 2 ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly TF_DIR="${ROOT_DIR}/infra/terraform/azure"

# Strict validation of required configuration files
if [[ ! -f "${TF_DIR}/environments/${ENVIRONMENT}/backend.hcl" ]]; then
  echo "Error: Missing backend configuration. Run phase 1 and 2 first." >&2
  exit 1
fi

readonly PLATFORM_VARS_FILE="${TF_DIR}/environments/${ENVIRONMENT}/platform.tfvars"
if [[ ! -f "${PLATFORM_VARS_FILE}" ]]; then
  echo "Error: Missing ${PLATFORM_VARS_FILE}." >&2
  exit 1
fi

# Dynamically extract the image tag from platform.tfvars
readonly CONTAINER_IMAGE_TAG=$(grep -E '^[[:space:]]*container_image_tag' "${PLATFORM_VARS_FILE}" | awk -F'=' '{gsub(/ /,"",$2); gsub(/"/,"",$2); print $2}')
if [[ -z "${CONTAINER_IMAGE_TAG:-}" ]]; then
  echo "Error: Could not parse container_image_tag from ${PLATFORM_VARS_FILE}" >&2
  exit 1
fi

cd "${TF_DIR}"
if [[ ! -d "${TF_DIR}/.terraform" ]]; then
  terraform init -reconfigure -backend-config="${TF_DIR}/environments/${ENVIRONMENT}/backend.hcl"
fi
#Read the proper output variable
readonly ACR_LOGIN_SERVER=$(terraform output -raw acr_login_server 2>/dev/null || true)
if [[ -z "${ACR_LOGIN_SERVER:-}" ]]; then
  echo "Error: Terraform output 'acr_login_server' is missing. Apply phase 2 and ensure outputs.tf exists." >&2
  exit 1
fi

# Guard against running outside the Jumpbox
echo "==> Authenticating to ACR..."
if az login --identity >/dev/null 2>&1; then
  echo "==> Authenticated with managed identity."
else
  echo "==> Managed identity login failed; falling back to existing local Azure credentials."
  if ! az account show >/dev/null 2>&1; then
    echo "Error: Azure authentication is not available. Run 'az login' or execute this script on the Azure Jumpbox." >&2
    exit 1
  fi
fi

if ! az acr login --name "${ACR_LOGIN_SERVER%%.*}" >/dev/null 2>&1; then
  echo "Error: Failed to login to ACR ${ACR_LOGIN_SERVER%%.*}" >&2
  exit 1
fi

echo "==> Building and pushing image..."
cd "${ROOT_DIR}"
docker build -t "${ACR_LOGIN_SERVER}/router-agent:${CONTAINER_IMAGE_TAG}" .
docker push "${ACR_LOGIN_SERVER}/router-agent:${CONTAINER_IMAGE_TAG}"

echo "==> Image push complete."