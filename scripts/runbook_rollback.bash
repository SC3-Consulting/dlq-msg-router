#!/usr/bin/env bash

##########################
# runbook_rollback.bash
# Rolls back a Container App to a previous revision.
# Usage:
#   ./scripts/runbook_rollback.bash <resource-group> <container-app-name> [target-revision]
# If target-revision is omitted, script selects the most recent INACTIVE revision.
# Example:
#   ./scripts/runbook_rollback.bash rg-dlq-msg-router-dev ca-dlq-msg-router-dev    
#
##########################


set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <resource-group> <container-app-name> [target-revision]"
  echo "If target-revision is omitted, script selects the most recent INACTIVE revision."
  echo "Example: $0 rg-dlq-msg-router-dev ca-dlq-msg-router-dev"
  exit 1
fi

RG="$1"
APP="$2"
TARGET_REVISION="${3:-}"

if ! command -v az >/dev/null 2>&1; then
  echo "ERROR: az CLI is required." >&2
  exit 2
fi

if [[ -z "$TARGET_REVISION" ]]; then
  TARGET_REVISION="$({
    az containerapp revision list --resource-group "$RG" --name "$APP" \
      --query "reverse(sort_by([?properties.active==\`false\`], &properties.createdTime))[0].name" \
      --output tsv
  } || true)"
fi

if [[ -z "$TARGET_REVISION" ]]; then
  echo "ERROR: no rollback target revision found. Provide one explicitly." >&2
  exit 3
fi

echo "Rolling back $APP in $RG to revision: $TARGET_REVISION"

az containerapp revision activate \
  --resource-group "$RG" \
  --name "$APP" \
  --revision "$TARGET_REVISION" \
  --output table

echo
echo "Current active revisions:"
az containerapp revision list --resource-group "$RG" --name "$APP" \
  --query "[?properties.active].{name:name,active:properties.active,trafficWeight:properties.trafficWeight,createdTime:properties.createdTime}" \
  --output table

echo "Rollback completed."
