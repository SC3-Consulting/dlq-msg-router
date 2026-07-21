# Stakeholder Demo Guide: Telemetry and Observability

This guide provides presentation-ready KQL queries for demonstrating the DLQ telemetry pipeline in Azure Log Analytics.

All JSON-based queries below use robust marker parsing:

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

## 0. Raw Telemetry Data Grid (The Excel View)

Purpose: provide engineers with the raw row-level telemetry view used for troubleshooting and CSV export.

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-dlq-msg-router-<env>"
| where Log_s startswith "CSV_EXPORT|"
| extend csv_string = substring(Log_s, 11)
| extend columns = parse_csv(csv_string)
| project 
	timestamp = columns[0],
	source_queue = columns[1],
	client_id = columns[2],
	message_type = columns[3],
	classification = columns[4],
	pattern = columns[5],
	status = columns[6],
	occurrence_count = columns[7],
	suggested_action = columns[8],
	confidence_score = columns[9]
| sort by todatetime(timestamp) desc
```

---

## 1. System Health and Throughput (Time Series)

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

## 2. AI vs Deterministic Routing (Pie Chart Data)

Purpose: compare routing outcomes by status (for example Auto_Classified vs AI_Suggested_Rule_Pending_Approval).

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

Purpose: show which operational actions are being recommended most frequently.

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

## 5. Queue Health (Processed and Failures by Queue)

Purpose: present per-queue workload and escalations for operational balancing.

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

## 6. Escalation Rate

Purpose: quantify the proportion of messages requiring escalation.

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

## 7. Failure Category Trend (Optional)

Purpose: trend top classifications over time for incident review.

```kusto
let appName = "ca-dlq-msg-router-<env>";
let lookback = 24h;
ContainerAppConsoleLogs_CL
| where TimeGenerated >= ago(lookback)
| where ContainerAppName_s == appName
| where Log_s contains "JSON_EXPORT|"
| extend evt = parse_json(substring(Log_s, indexof(Log_s, "JSON_EXPORT|") + 12))
| extend classification = tostring(evt.classification)
| where isnotempty(classification)
| summarize total = count() by classification, bin(TimeGenerated, 5m)
| order by TimeGenerated asc
```

---

## Demo Delivery Notes

1. Start with the raw telemetry grid for troubleshooting transparency.
2. Move to throughput and routing status to explain system behaviour under load.
3. Use queue health and action distribution to connect outcomes to operational decisions.
4. Close with escalation rate and AI confidence to discuss governance and quality.
