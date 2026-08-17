#!/usr/bin/env bash

##########################
# 04-push-image.bash
# Builds and pushes the Router Agent image to ACR
#
# Usage:
#   ./infra/scripts/04-push-image.bash -e dev
##########################
set -euo pipefail

ENVIRONMENT="${TARGET_ENV:-dev}"
while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--environment)
      if [[ $# -lt 2 || -z "${2:-}" ]]; then
        echo "Error: --environment requires a non-empty value (for example: dev, test, prod)." >&2
        exit 1
      fi
      ENVIRONMENT="$2"
      shift 2
      ;;
    *) echo "Unknown option: $1" >&2; exit 1 ;;
  esac
done

if [[ -z "${ENVIRONMENT}" ]]; then
  echo "Error: Target environment is empty. Set TARGET_ENV (for example: export TARGET_ENV=dev) or pass -e dev." >&2
  exit 1
fi

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly TF_DIR="${ROOT_DIR}/infra/terraform/azure"
readonly JUMPBOX_AUTH_FILE="${ROOT_DIR}/.azure/jumpbox-auth.env"

# Strict validation of required configuration files
if [[ ! -f "${TF_DIR}/environments/${ENVIRONMENT}/backend.hcl" ]]; then
  echo "Error: Missing backend configuration. Run phase 1 and 2 first, then rerun 03-configure-jumpbox.sh from your local workstation to sync generated Terraform files onto the jumpbox." >&2
  echo "Hint: Resolved environment='${ENVIRONMENT}'. If you invoked '-e \"\${TARGET_ENV}\"', ensure TARGET_ENV is exported on this shell." >&2
  exit 1
fi

readonly PLATFORM_VARS_FILE="${TF_DIR}/environments/${ENVIRONMENT}/platform.tfvars"
if [[ ! -f "${PLATFORM_VARS_FILE}" ]]; then
  echo "Error: Missing ${PLATFORM_VARS_FILE}." >&2
  exit 1
fi

if [[ -f "${JUMPBOX_AUTH_FILE}" ]]; then
  # shellcheck disable=SC1090
  source "${JUMPBOX_AUTH_FILE}"
fi

readonly AZURE_CLIENT_ID="${AZURE_CLIENT_ID:-}"

# Dynamically extract the immutable image tags from platform.tfvars.
readonly ROUTER_CONTAINER_IMAGE_TAG=$(grep -E '^[[:space:]]*router_container_image_tag' "${PLATFORM_VARS_FILE}" | awk -F'=' '{gsub(/ /,"",$2); gsub(/"/,"",$2); print $2}')
readonly NOTIFICATION_CONTAINER_IMAGE_TAG=$(grep -E '^[[:space:]]*notification_container_image_tag' "${PLATFORM_VARS_FILE}" | awk -F'=' '{gsub(/ /,"",$2); gsub(/"/,"",$2); print $2}')
if [[ -z "${ROUTER_CONTAINER_IMAGE_TAG:-}" || -z "${NOTIFICATION_CONTAINER_IMAGE_TAG:-}" ]]; then
  echo "Error: Could not parse router_container_image_tag and notification_container_image_tag from ${PLATFORM_VARS_FILE}" >&2
  exit 1
fi

# Guard against running outside the Jumpbox
echo "==> Authenticating to ACR..."
if [[ -z "${AZURE_CLIENT_ID}" ]]; then
  echo "Error: Missing AZURE_CLIENT_ID for jumpbox managed identity login. Ensure ${JUMPBOX_AUTH_FILE} exists (rerun 03-configure-jumpbox.sh)." >&2
  exit 1
fi

if az login --identity --client-id "${AZURE_CLIENT_ID}" >/dev/null 2>&1; then
  echo "==> Authenticated with managed identity ${AZURE_CLIENT_ID}."
elif az login --identity --username "${AZURE_CLIENT_ID}" >/dev/null 2>&1; then
  echo "==> Authenticated with managed identity ${AZURE_CLIENT_ID}."
else
  echo "Error: Managed identity login failed for client ID ${AZURE_CLIENT_ID}." >&2
  echo "Hint: Run this command on the jumpbox for diagnostic output:" >&2
  echo "      az login --identity --client-id ${AZURE_CLIENT_ID}" >&2
  exit 1
fi

cd "${TF_DIR}"
export ARM_USE_MSI=true
export ARM_CLIENT_ID="${AZURE_CLIENT_ID}"
terraform init -reconfigure -backend-config="${TF_DIR}/environments/${ENVIRONMENT}/backend.hcl"

# Read the proper output variable after Azure authentication is established.
readonly ACR_LOGIN_SERVER=$(terraform output -raw acr_login_server 2>/dev/null || true)
if [[ -z "${ACR_LOGIN_SERVER:-}" ]]; then
  echo "Error: Terraform output 'acr_login_server' is missing. Apply phase 2 and ensure outputs.tf exists." >&2
  exit 1
fi

if ! az acr login --name "${ACR_LOGIN_SERVER%%.*}" >/dev/null 2>&1; then
  echo "Error: Failed to login to ACR ${ACR_LOGIN_SERVER%%.*}" >&2
  exit 1
fi

echo "==> Building and pushing image..."
cd "${ROOT_DIR}"
docker build \
  -t "${ACR_LOGIN_SERVER}/router-agent:${ROUTER_CONTAINER_IMAGE_TAG}" \
  -t "${ACR_LOGIN_SERVER}/router-agent:${NOTIFICATION_CONTAINER_IMAGE_TAG}" \
  .
docker push "${ACR_LOGIN_SERVER}/router-agent:${ROUTER_CONTAINER_IMAGE_TAG}"
docker push "${ACR_LOGIN_SERVER}/router-agent:${NOTIFICATION_CONTAINER_IMAGE_TAG}"

echo "==> Image push complete."