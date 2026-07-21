#!/bin/sh
set -eu

if [ -z "${LOG_ANALYTICS_WORKSPACE_ID:-}" ]; then
  echo "LOG_ANALYTICS_WORKSPACE_ID is required" >&2
  exit 1
fi

mkdir -p /var/lib/grafana/dashboards

for template in /etc/grafana/dashboard-templates/*.json; do
  [ -e "$template" ] || continue
  output="/var/lib/grafana/dashboards/$(basename "$template")"
  sed "s|@@LOG_ANALYTICS_WORKSPACE_ID@@|${LOG_ANALYTICS_WORKSPACE_ID}|g" "$template" > "$output"
done

exec /run.sh
