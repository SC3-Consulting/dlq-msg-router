FROM grafana/grafana:11.2.2

COPY --chmod=755 scripts/render-and-run.sh /etc/grafana/scripts/render-and-run.sh
COPY provisioning/ /etc/grafana/provisioning/
COPY dashboards/ /etc/grafana/dashboard-templates/
