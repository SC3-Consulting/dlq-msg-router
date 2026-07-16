# Stakeholder Demo Guide: Telemetry & Observability

This guide provides presentation-ready KQL queries for demonstrating the DLQ telemetry pipeline in Azure Log Analytics.

All queries below are intentionally based on `JSON_EXPORT|` events and use robust marker parsing:

```kusto
| extend evt = parse_json(substring(Log_s, indexof(Log_s, "JSON_EXPORT|") + 12))
```

## Shared Parameters

Set these once, then run each section query.

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
```

---

## 1. System Health & Throughput (Time Series)

Purpose: show total processed message volume over time to demonstrate sustained handling.

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
ContainerAppConsoleLogs_CL
| where TimeGenerated >= ago(lookback)
| where ContainerAppName_s == appName
| where Log_s contains "JSON_EXPORT|"
| extend evt = parse_json(substring(Log_s, indexof(Log_s, "JSON_EXPORT|") + 12))
| summarize total_processed = count() by bin(TimeGenerated, 5m)
| order by TimeGenerated asc
```

---

## 2. AI vs. Deterministic Routing (Pie Chart Data)

Purpose: compare routing outcomes by `status` (for example `Auto_Classified` vs `AI_Suggested_Rule_Pending_Approval`).

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
ContainerAppConsoleLogs_CL
| where TimeGenerated >= ago(lookback)
| where ContainerAppName_s == appName
| where Log_s contains "JSON_EXPORT|"
| extend evt = parse_json(substring(Log_s, indexof(Log_s, "JSON_EXPORT|") + 12))
| extend status = tostring(evt.status)
| where isnotempty(status)
| summarize total = count() by status
| order by total desc
```

---

## 3. Action Distribution

Purpose: show what operational actions are being recommended most frequently.

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
ContainerAppConsoleLogs_CL
| where TimeGenerated >= ago(lookback)
| where ContainerAppName_s == appName
| where Log_s contains "JSON_EXPORT|"
| extend evt = parse_json(substring(Log_s, indexof(Log_s, "JSON_EXPORT|") + 12))
| extend suggested_action = tostring(evt.suggested_action)
| where isnotempty(suggested_action)
| summarize total = count() by suggested_action
| order by total desc
```

---

## 4. AI Confidence Overview

Purpose: quantify AI confidence by classification using explicit numeric casting.

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
ContainerAppConsoleLogs_CL
| where TimeGenerated >= ago(lookback)
| where ContainerAppName_s == appName
| where Log_s contains "JSON_EXPORT|"
| extend evt = parse_json(substring(Log_s, indexof(Log_s, "JSON_EXPORT|") + 12))
| extend status = tostring(evt.status)
| where status contains "AI"
| extend classification = tostring(evt.classification)
| extend confidence_score = todouble(tostring(evt.confidence_score))
| where isnotnull(confidence_score)
| summarize avg_confidence_score = avg(confidence_score), sample_size = count() by classification
| order by avg_confidence_score desc
```

---

## Demo Delivery Notes

1. Start with the throughput chart to establish pipeline stability under load.
2. Move to status pie chart to explain deterministic vs AI-assisted routing behavior.
3. Use action distribution to connect decisions to operator workflows.
4. Close with confidence overview to discuss model reliability by classification pattern.
