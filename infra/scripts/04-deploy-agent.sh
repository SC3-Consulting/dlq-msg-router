#!/usr/bin/env bash

###############################################################################
# 04-deploy-agent.sh
#
# SYNOPSIS
#   Deploys computing layers and bindings for the active agent orchestrator.
#
# DESCRIPTION
#   Targets solely the agent hosting compute workspace blocks, leaving 
#   underlying platform network paths and private links unperturbed.
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
      echo "[-] Unknown execution parameter: $1" >&2
      exit 1
      ;;
  esac
done

readonly SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
readonly ROOT_DIR="$(cd "${SCRIPT_DIR}/../.." && pwd)"
readonly TF_DIR="${ROOT_DIR}/infra/terraform/azure"
readonly BOOTSTRAP_VARS_FILE="${TF_DIR}/environments/${ENVIRONMENT}/bootstrap.generated.tfvars"
readonly PLATFORM_VARS_FILE="${TF_DIR}/environments/${ENVIRONMENT}/platform.tfvars"

# Enforce script constraint checkpoints
if [[ ! -f "${PLATFORM_VARS_FILE}" ]]; then
  echo "[-] Error: Expected environment runtime parameter configuration file '$PLATFORM_VARS_FILE' is absent." >&2
  exit 1
fi

if [[ ! -f "${BOOTSTRAP_VARS_FILE}" ]]; then
  echo "[-] Error: State generation map '$BOOTSTRAP_VARS_FILE' is missing. Re-execute Phase 1." >&2
  exit 1
fi

echo "==> Phase 4: Constructing isolated computing layers for Agent Hosting Modules..."
cd "${TF_DIR}"

readonly COMPUTE_ARGS=(
  "apply" "-auto-approve"
  "-target=module.agent_hosting"
  "-var-file=${PLATFORM_VARS_FILE}"
  "-var-file=${BOOTSTRAP_VARS_FILE}"
  "-var=environment=${ENVIRONMENT}"
)
terraform "${COMPUTE_ARGS[@]}"

echo "[+] Phase 4 Execution Concluded. DLQ Routing Orchestrator is operational in ACA workspace."