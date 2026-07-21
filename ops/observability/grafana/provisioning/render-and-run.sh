#!/bin/sh
set -eu

if [ -z "${LOG_ANALYTICS_WORKSPACE_ID:-}" ]; then
  echo "LOG_ANALYTICS_WORKSPACE_ID is required" >&2
  exit 1
fi

RUNTIME_DASHBOARD_DIR="/tmp/grafana-dashboards"
mkdir -p "${RUNTIME_DASHBOARD_DIR}"

for template in /etc/grafana/dashboard-templates/*.json; do
  [ -e "$template" ] || continue
  output="${RUNTIME_DASHBOARD_DIR}/$(basename "$template")"
  sed "s|@@LOG_ANALYTICS_WORKSPACE_ID@@|${LOG_ANALYTICS_WORKSPACE_ID}|g" "$template" > "$output"
done

exec /run.sh
