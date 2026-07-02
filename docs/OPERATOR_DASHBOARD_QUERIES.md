# Operator Dashboard Query Pack

This file contains ready-to-use KQL queries for the core operational dashboard views:

- Queue health
- DLQ trend
- Action distribution
- Escalation rates

All queries assume Azure Container Apps logs are stored in `ContainerAppConsoleLogs_CL` and the agent writes structured events with `JSON_EXPORT|` prefix.

## Shared Parameters

Set these in Log Analytics before running queries:

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
```

`<env>` can be one of: 
- dev
- test 
- prod

---

## 1) Queue Health (Processed + Failures by Queue)

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
ContainerAppConsoleLogs_CL
| where TimeGenerated >= ago(lookback)
| where ContainerAppName_s == appName
| where Log_s contains "JSON_EXPORT|"
| extend evt = parse_json(substring(Log_s, indexof(Log_s, "JSON_EXPORT|") + 12))
| extend source_queue = tostring(evt.source_queue)
| extend status = tostring(evt.status)
| summarize
    processed = count(),
    escalated = countif(tolower(tostring(evt.suggested_action)) == "escalate"),
    quarantined = countif(status == "Quarantined")
  by source_queue
| order by processed desc
```

---

## 2) DLQ Trend (Message Volume Over Time)

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
ContainerAppConsoleLogs_CL
| where TimeGenerated >= ago(lookback)
| where ContainerAppName_s == appName
| where Log_s contains "JSON_EXPORT|"
| summarize messages = count() by bin(TimeGenerated, 5m)
| order by TimeGenerated asc
```

---

## 3) Action Distribution

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
ContainerAppConsoleLogs_CL
| where TimeGenerated >= ago(lookback)
| where ContainerAppName_s == appName
| where Log_s contains "JSON_EXPORT|"
| extend evt = parse_json(substring(Log_s, indexof(Log_s, "JSON_EXPORT|") + 12))
| extend action = tostring(evt.suggested_action)
| where isnotempty(action)
| summarize total = count() by action
| order by total desc
```

---

## 4) Escalation Rate

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
ContainerAppConsoleLogs_CL
| where TimeGenerated >= ago(lookback)
| where ContainerAppName_s == appName
| where Log_s contains "JSON_EXPORT|"
| extend evt = parse_json(substring(Log_s, indexof(Log_s, "JSON_EXPORT|") + 12))
| summarize
    total_messages = count(),
    escalated_messages = countif(tolower(tostring(evt.suggested_action)) == "escalate")
| extend escalation_rate_pct = iif(total_messages == 0, 0.0, todouble(escalated_messages) * 100.0 / todouble(total_messages))
```

---

## Optional: Failure Category Trend

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
ContainerAppConsoleLogs_CL
| where TimeGenerated >= ago(lookback)
| where ContainerAppName_s == appName
| where Log_s has "failure_"
| summarize lines = count() by bin(TimeGenerated, 5m)
| order by TimeGenerated asc
```
