#!/usr/bin/env bash

###############################################################################
# 99-destroy-all.sh
#
# SYNOPSIS
#   Tears down compute, data, and network planes to mitigate idle billing costs.
#
# DESCRIPTION
#   Executes standard clean state destruction. Provides an optional --full-purge
#   flag to completely clear out root bootstrap storage states.
#
# PARAMETERS
#   -e, --environment   Target deployment environment (dev, test, prod). Default: dev
#   --full-purge        Forceful extraction and deletion of bootstrap storage states.
###############################################################################

set -euo pipefail

ENVIRONMENT="dev"
FULL_PURGE=false

while [[ $# -gt 0 ]]; do
  case "$1" in
    -e|--environment)
      ENVIRONMENT="$2"
      shift 2
      ;;
    --full-purge)
      FULL_PURGE=true
      shift
      ;;
    *)
      echo "[-] Unknown flag option passed: $1" >&2
      exit 1
      ;;
  esac
done

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly TF_DIR="${ROOT_DIR}/infra/terraform/azure"
readonly BOOTSTRAP_VARS_FILE="${TF_DIR}/environments/${ENVIRONMENT}/bootstrap.generated.tfvars"
readonly PLATFORM_VARS_FILE="${TF_DIR}/environments/${ENVIRONMENT}/platform.tfvars"

echo "[!] WARNING: Initiating teardown of ephemeral compute, network data nodes, and private links..."
cd "${TF_DIR}"

readonly DESTROY_ARGS=(
  "destroy" "-auto-approve"
  "-var-file=${PLATFORM_VARS_FILE}"
  "-var-file=${BOOTSTRAP_VARS_FILE}"
  "-var=environment=${ENVIRONMENT}"
)
terraform "${DESTROY_ARGS[@]}"

# Perform complete bootstrap storage group purge if explicitly requested (Resolves Destroy TODO Requirement)
if [ "${FULL_PURGE}" = true ]; then
  echo "[!] CRITICAL: Full purge requested. Eradicating state resource group boundaries..."
  readonly RG_TARGET="rg-viva-dlq-${ENVIRONMENT}"
  if az group exists --name "${RG_TARGET}" &>/dev/null; then
    az group delete --name "${RG_TARGET}" --yes --no-wait
    echo "[+] Group erasure request queued on Azure plane."
  fi
fi

echo "[+] Teardown cycles successfully executed."