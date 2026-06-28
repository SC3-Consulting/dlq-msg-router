#!/usr/bin/env bash

##########################
# runbook_triage_snapshot.bash
# Captures a snapshot of the current state of a Container App for triage purposes.
# Usage:
#   ./scripts/runbook_triage_snapshot.bash <resource-group> <container-app-name> [minutes]
# If minutes is omitted, defaults to 60 minutes of log history.
# Example:
#   ./scripts/runbook_triage_snapshot.bash rg-dlq-msg-router-dev ca-dlq-msg-router-dev      
#
##########################

set -euo pipefail

if [[ $# -lt 2 ]]; then
  echo "Usage: $0 <resource-group> <container-app-name> [minutes]"
  echo "Example: $0 rg-dlq-msg-router-dev ca-dlq-msg-router-dev 60"
  exit 1
fi

RG="$1"
APP="$2"
MINUTES="${3:-60}"

if ! command -v az >/dev/null 2>&1; then
  echo "ERROR: az CLI is required." >&2
  exit 2
fi

TMP_DIR="reports/triage"
mkdir -p "$TMP_DIR"
TS="$(date +%Y%m%d-%H%M%S)"
OUT_FILE="$TMP_DIR/triage-${APP}-${TS}.txt"

{
  echo "=== DLQ Agent Triage Snapshot ==="
  echo "timestamp_utc=$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  echo "resource_group=$RG"
  echo "container_app=$APP"
  echo "lookback_minutes=$MINUTES"
  echo

  echo "--- Container App State ---"
  az containerapp show --resource-group "$RG" --name "$APP" \
    --query "{name:name,location:location,provisioningState:properties.provisioningState,runningStatus:properties.runningStatus,latestRevisionName:properties.latestRevisionName}" \
    --output table
  echo

  echo "--- Active Revisions ---"
  az containerapp revision list --resource-group "$RG" --name "$APP" \
    --query "[?properties.active].{name:name,active:properties.active,trafficWeight:properties.trafficWeight,createdTime:properties.createdTime,healthState:properties.healthState}" \
    --output table
  echo

  echo "--- Recent Logs ---"
  az containerapp logs show --resource-group "$RG" --name "$APP" --tail 150
} > "$OUT_FILE" 2>&1

echo "Triage snapshot written to $OUT_FILE"
