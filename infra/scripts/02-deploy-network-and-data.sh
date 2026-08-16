#!/usr/bin/env bash

###############################################################################
# 02-deploy-network-and-data.sh
#
# SYNOPSIS
#   Deploys network topology and core data planes for the router environment.
#
# DESCRIPTION
#   Validates dependency presence and applies module layers under a targeted
#   execution boundary. Preserves strict isolated var-file constraints.
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
readonly PLATFORM_VARS_FILE="${TF_DIR}/environments/${ENVIRONMENT}/platform.tfvars"

# Enforce strict input verification patterns
for f in "${BACKEND_FILE}" "${BOOTSTRAP_VARS_FILE}" "${PLATFORM_VARS_FILE}"; do
  if [[ ! -f "$f" ]]; then
    echo "[-] Error: Required structural input file '$f' is missing from the directory context." >&2
    exit 1
  fi
done

echo "==> Phase 2: Initialising Network & Data Plane Storage Workspaces..."
cd "${TF_DIR}"
rm -rf .terraform

readonly INIT_ARGS=(
  "init" "-reconfigure" "-backend-config=${BACKEND_FILE}"
)
terraform "${INIT_ARGS[@]}"

echo "==> Phase 2: Executing targeted core module infrastructure rollout..."
readonly APPLY_ARGS=(
  "apply" "-auto-approve"
  "-target=module.foundation"
  "-target=module.network"
  "-target=module.dns"
  "-target=module.observability"
  "-target=module.data_services"
  "-target=azurerm_key_vault.webhook_secrets"
  "-target=azurerm_role_assignment.webhook_secrets_deployer"
  "-target=module.foundry"
  "-target=module.private_endpoints"
  "-target=module.identity"
  "-target=module.bastion_jumpbox"
  "-var-file=${PLATFORM_VARS_FILE}"
  "-var-file=${BOOTSTRAP_VARS_FILE}"
  "-var=environment=${ENVIRONMENT}"
)
terraform "${APPLY_ARGS[@]}"

echo "[+] Phase 2 Complete. Secure Core Network, Data planes, and Jumpbox units are online."