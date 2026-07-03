# Operational Runbook: DLQ Triage & Platform Dependencies

This guide details the Day 2 operational procedures for managing the Autonomous Dead Letter Queue (DLQ) agent. It covers telemetry extraction, synthetic traffic simulation, cloud cost controls, pipeline maintenance, and common troubleshooting patterns encountered in production.

## 1. Telemetry Dashboard & Log Analytics Extraction

Azure Container Apps (ACA) utilise ephemeral storage. Due to strict Role-Based Access Control (RBAC) mandates, operators are not granted the `Microsoft.App/containerApps/getAuthToken/action` permissions required to physically extract dashboard files (e.g., `telemetry_dashboard.csv`) via the Azure CLI.

**The Report-to-Log Pattern:**
To securely extract operational metrics without breaching least-privilege principles, the agent broadcasts structured CSV rows directly to standard output, prefixed with `CSV_EXPORT|`. 

The agent also emits structured JSON contracts prefixed with `JSON_EXPORT|` for easier machine parsing and correlation in log pipelines.

To generate the operational dashboard, navigate to the Azure Log Analytics Workspace linked to the container environment and execute the following Kusto Query Language (KQL) script. Select 'Export to CSV' from the portal interface to download the results.

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-dlq-msg-router-dev"
| where Log_s startswith "CSV_EXPORT|"
| extend csv_string = substring(Log_s, 11)
| extend columns = split(csv_string, ",")
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

### Structured JSON Event Query

Use this KQL query to parse the JSON event stream and inspect action/status distributions without CSV string-splitting:

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-dlq-msg-router-dev"
| where Log_s startswith "JSON_EXPORT|"
| extend json_payload = parse_json(substring(Log_s, 12))
| project
   TimeGenerated,
   source_queue = tostring(json_payload.source_queue),
   client_id = tostring(json_payload.client_id),
   classification = tostring(json_payload.classification),
   pattern = tostring(json_payload.pattern),
   status = tostring(json_payload.status),
   suggested_action = tostring(json_payload.suggested_action)
| sort by TimeGenerated desc
```

### Runtime Metrics Endpoint

The runtime exposes an in-process metrics endpoint at `/metrics` on the same probe host/port as health checks (default `:8080`).

- Local check: `curl http://127.0.0.1:8080/metrics`
- Response: JSON document with aggregate counters (`messages_processed_total`, `retries_total`, `escalations_total`, `cache_hits_total`, `ai_calls_total`, `failures_total`) and per-queue counts.

### Correlation Model (Mixed OTel and Non-OTel Traffic)

DLQ events may originate from API Management, Azure Container Apps, or custom producers. Some messages may include OpenTelemetry context, others may only include broker identifiers.

The agent applies this precedence when building correlation context:

1. `traceparent` / `Diagnostic-Id` (W3C context)
2. explicit trace identifiers (`trace_id`, `x-b3-traceid`, `otel.trace_id`)
3. broker `correlation_id`
4. broker `message_id`

Structured JSON events include the following fields when available:

- `correlation_id`
- `trace_id`
- `span_id`
- `traceparent`
- `tracestate`
- `diagnostic_id`
- `correlation_source`

Idempotency uses trace-aware anchors. When a valid trace is present, duplicate detection anchors on trace identity before falling back to broker correlation identifiers.

## 2. Platform Dependencies & Simulation (Local / Jumpbox)

### Optional: Azure Service Bus Emulator (Opt-In Local Build)

For local, repeatable message-flow testing without Azure credentials, this repository supports the official Azure Service Bus emulator as an opt-in Docker Compose profile.

Resource note: the emulator stack starts both the Service Bus emulator and SQL Server. Short-lived spikes in CPU, RAM, and disk activity are normal during startup. In Docker-outside-of-Docker or WSL-backed devcontainers, these spikes can be large enough to disrupt editor connectivity.

1. Set local `.env` values (or export shell vars) before startup:

   ```bash
   ACCEPT_EULA="Y"
   MSSQL_SA_PASSWORD="YourStrong!Passw0rd"
   SERVICE_BUS_CONNECTION_STRING="Endpoint=sb://servicebus-emulator;SharedAccessKeyName=RootManageSharedAccessKey;SharedAccessKey=SAS_KEY_VALUE;UseDevelopmentEmulator=true;"
   ENABLE_DYNAMIC_DISCOVERY="False"
   ```

   For a lower-footprint local session, also prefer:

   ```bash
   OLLAMA_MODEL="qwen2.5:0.5b"
   MAX_CONCURRENT_QUEUES=1
   PREFETCH_COUNT=1
   AGENT_CYCLE_SLEEP_SECONDS=30
   ```

2. Start the stack with emulator profile enabled:

   ```bash
   make local-up-emulator
   ```

3. Verify emulator health endpoint:

   ```bash
   curl http://127.0.0.1:5300/health
   ```

4. Verify agent health and metrics endpoints:

   ```bash
   curl http://127.0.0.1:8080/health
   curl http://127.0.0.1:8080/metrics
   ```

5. Run the local emulator smoke test:

   ```bash
   make local-smoke-emulator
   ```

The smoke test sends a message, dead-letters it, and verifies that the agent processes the injected DLQ message by observing `messages_processed_total` on `/metrics`.

The smoke command executes inside the running `dlq-agent` container to avoid host networking ambiguity in Docker-outside-of-Docker environments.

If the devcontainer remains unstable under emulator load, run the emulator stack from the host shell rather than from inside the VS Code devcontainer.

In Docker-outside-of-Docker environments, the local Compose stack uses Docker named volumes for agent `data` and `reports` paths to avoid host bind-mount path/permission mismatches.

The emulator entity configuration for this repository is tracked at `infra/local/servicebus-emulator/config.json`, baked into the local emulator image, and includes `integration-queue`, `payments-queue`, and `parking-lot-queue`.

If executing without live upstream traffic, operators must generate synthetic workloads to validate heuristic gates and AI fallback pathways.

### Setting up the ASB Simulators
Ensure you execute these commands from the repository root to avoid `ModuleNotFoundError` execution isolation traps.

1. **Sanitise the Environment:** Before initiating a test cycle, flush all ghost messages or orphaned payloads from the targeted Service Bus namespaces.

   ```bash
   python -m src.flush_queues
   ```
2. **Start the Consumer Emulator:** Simulates downstream outages and application crashes.

   ```bash
   python -m simulator.consumer
   ```
3. **Start the Producer Emulator:** Dispatches synthetic anomalies, malformed schemas, and duplicate correlation IDs to trigger the classification gates.

   ```bash
   python -m simulator.producer
   ```

### Setting up Azure AI Foundry
The production agent utilises Azure AI Foundry. It connects passwordless via `DefaultAzureCredential`. Ensure the container's User-Assigned Managed Identity is granted the `Cognitive Services OpenAI User` role on the Foundry resource. Required environment variables: `AI_PROVIDER="AZURE_FOUNDRY"`, `AZURE_FOUNDRY_ENDPOINT`, and `AZURE_FOUNDRY_DEPLOYMENT_NAME`.

**Automation Note:** This configuration is fully automated. The Phase 2 deployment module provisions the required `Cognitive Services OpenAI User` role bindings. Furthermore, the Phase 3 (`05-configure-jumpbox.sh`) and Phase 4 (`04-deploy-agent.sh`) bash scripts dynamically extract the `AZURE_FOUNDRY_ENDPOINT` from the remote state and inject it into the local `.env` and Container App environments, preventing manual configuration drift.

## 3. Cloud Cost Control & Infrastructure Teardown

When deployed to an Azure environment for testing or staging, operators must manage the infrastructure lifecycle to ensure zero cost overnight when pipeline testing is halted. 

### The Fixed-Cost Billing Reality
While the Azure Container Apps (ACA) compute layer can scale to zero, the underlying architecture relies on **Azure Service Bus (Premium Tier)** and **Azure Bastion**. These enterprise data plane services carry a substantial fixed hourly base cost regardless of active throughput or connected replicas. 

Manually scaling the Container App to zero via the Azure CLI **will not** stop the billing meters for the network and messaging infrastructure.

To successfully halt billing overnight, you must explicitly destroy the ephemeral compute and networking components via the Terraform orchestrator. This script surgically removes the expensive resources whilst preserving the Remote State (Storage Account) and Key Vault for rapid redeployment.

**Halt Billing (Destroy Ephemeral Infrastructure):**
Execute this from your local machine (not the Jumpbox) to destroy the network and compute resources while preserving the Terraform state:

```bash
bash ./infra/scripts/99-destroy-all.sh -e <env>
```

*(Architectural Note: To execute a complete eradication, including the state storage and Key Vault, append the `--full-purge` flag).*

**Resume Operations:**
Execute Phase 2 and Phase 4 from the `DEPLOYMENT_RUNBOOK.md` (located in the repository docs folder) to restore the network and agent.

## 4. The Human-in-the-Loop Concept & Parking Lot Remediation

The agent is strictly governed. Crucially, **the AI is not permitted to alter the message or execute a destructive action on novel errors.** Unknown anomalies are sent to Azure Foundry for classification, routed to the `parking-lot-queue`, and flagged as `AI_Suggested_Rule_Pending_Approval`.

### Remediation Workflow

1. **Assess the AI Recommendation:** Review the telemetry dashboard to identify the suggested pattern and action provided by the AI.
2. **Approve or Reject:** If the AI hallucinated, drop or manually retry the message via Azure Service Bus Explorer. If valid, proceed to codify the rule.
3. **Update the Heuristics Engine:** A platform engineer must manually codify the AI's recommendation into `data/rules.json`.
   ```json
   {
     "rule_id": "app_005",
     "severity_score": 40,
     "classification": "Schema_Validation_Failed",
     "pattern_name": "missing_transaction_date",
     "condition": "reason == 'ValidationFailed' and client_id == 'Alpha_Corp'",
     "default_action": "fix_and_retry",
     "safe_defaults_map": {
       "transaction_date": "1970-01-01T00:00:00Z"
     }
   }
   ```
4. **Reload & Resubmit:** Restart the container to flush the in-memory classification cache and load the new `rules.json`. Resubmit the pending messages from the `parking-lot-queue` to their upstream queues. The deterministic engine will now intercept them natively, bypassing AI token costs.

## 5. Concurrency, Thread Management, & Idempotency

The agent utilises a bounded concurrency model to handle hundreds of queues without network exhaustion or OS-level thread churn.

### AMQP Connection Multiplexing
The Azure Service Bus Python SDK is not inherently thread-safe. Attempting to instantiate separate client connections across multiple polling threads triggers rapid socket exhaustion (`amqp:connection:forced`). 
To resolve this, the pipeline utilises a Singleton `ServiceBusClientFactory` shielded by `threading.Lock()`. This multiplexes a single, resilient underlying AMQP 1.0 network connection across all active worker threads.

### Thread Pool Elevation
The `ThreadPoolExecutor` is elevated *outside* the primary `while` loop within `src/run_agent.py`. This architectural decision prevents aggressive thread creation and destruction every 60 seconds, drastically reducing the container's CPU overhead.

### Idempotency Daemon
The Idempotency Store (Gate B) relies on a local SQLite/DBM database to block exact correlation match duplicates. To prevent this ephemeral cache from causing Disk Full crashes in high-throughput environments, a background `disk_cleanup_daemon` thread wakes hourly to automatically purge expired cache keys based on the `IDEMPOTENCY_TTL_SECONDS` variable.

## 6. Known Failure States & Troubleshooting

### 1. The Identity Trap (`ClientAuthenticationError`)
**Symptom:** The agent container crash-loops on startup, logging `ClientAuthenticationError` or `400 invalid_scope`.
**Root Cause:** The `DefaultAzureCredential` SDK defaults to seeking a System-Assigned Managed Identity. Because the infrastructure utilises a User-Assigned Managed Identity to decouple RBAC assignments from the container lifecycle and prevent IaC race conditions, the SDK fails to authenticate.
**Resolution:** Ensure `AZURE_CLIENT_ID` is populated in the container's environment variables with the exact Client ID of the User-Assigned Identity. The SDK will automatically prioritise this variable.

Proof of successful User-Assigned token acquisition from the container trace:
```text
2026-06-14T21:37:33.3080784Z - INFO - [ThreadPoolExecutor-0_0] - AppServiceCredential.get_token succeeded
2026-06-14T21:37:33.3081134Z - INFO - [ThreadPoolExecutor-0_0] - ManagedIdentityCredential.get_token succeeded
2026-06-14T21:37:33.3081274Z - INFO - [ThreadPoolExecutor-0_0] - DefaultAzureCredential acquired a token from ManagedIdentityCredential
```

### 2. The Split-Brain Typo (`amqp:not-found`)
**Symptom:** The simulators crash immediately upon execution with an AMQP not found exception.
**Root Cause:** The Terraform infrastructure provisioned a specific queue (e.g., `payments-queue`), but local fallback JSON configurations (`data/asb_sources.json`) contain a typo (e.g., `payments-queue` vs `integration-queue`).
**Resolution:** Enable dynamic topology mapping by setting `ENABLE_DYNAMIC_DISCOVERY="True"` in the `.env` file. This forces the agent and simulators to query the `ServiceBusAdministrationClient` to programmatically discover the true state of the namespace, bypassing static JSON typos entirely.*(Note: The `05-configure-jumpbox.sh` provisioning script natively injects this setting to protect Jumpbox simulators automatically).*

### 3. AI Inference Causes AMQP Connection Drops (Local Dev Only)
**Symptom:** The local agent logs `amqp:connection:forced` followed by a cascade of `ValueError: Link already closed` errors.
**Root Cause:** When running locally against an offline LLM (Ollama), inference may consume 100% of the CPU, starving the Azure AMQP heartbeat thread. Azure detects the dropped heartbeat and severs the remote connection.
**Resolution:** Downgrade the local Ollama model to a lower parameter count (e.g., `qwen2.5:0.5b`) to reduce inference latency below the Azure timeout threshold. If GPU and VRAM are available, use a larger model matched to memory capacity for better classification quality: `qwen2.5:7b-instruct` or `llama3.1:8b-instruct-q4_K_M` (>= 8 GB VRAM), `qwen2.5:14b-instruct-q4_K_M` (>= 16 GB VRAM). This issue does not occur in production as execution is routed to the external Azure AI Foundry endpoint.

## 7. Quality Gates & PII Handling

* **Formatting & Linting:** Run `black`, `isort`, and `pylint` (minimum score 8.0) across the `src/` directory before merging code.
* **Testing:** Run `pytest -v --cov=src` to validate core logic. The pipeline enforces an 85% coverage minimum.
* **PII Masking:** Before transmission to Azure Foundry, payloads are dynamically scrubbed. Phone numbers, emails, and valid Luhn-checksum credit cards are replaced with redacted placeholders. Standard 16-digit correlation IDs are preserved for telemetry debugging.
* **JSON Salvage:** To prevent pipeline-crashing `JSONDecode` errors resulting from LLM formatting hallucinations, the `ai_client.py` executes a defensive regex salvage protocol to strip unwanted markdown code blocks prior to parsing.

## 8. Resource Limits, Scaling Defaults, and Safe Operating Ranges

This section defines the default compute profile and queue concurrency envelope used by deployment manifests.

### Container App Defaults (Terraform)

Per-environment defaults are set in `infra/terraform/azure/environments/*/platform.tfvars`:

- `dev`: `agent_container_cpu=0.5`, `agent_container_memory=1Gi`, `agent_min_replicas=1`, `agent_max_replicas=1`
- `test`: `agent_container_cpu=0.5`, `agent_container_memory=1Gi`, `agent_min_replicas=1`, `agent_max_replicas=1`
- `prod`: `agent_container_cpu=1`, `agent_container_memory=2Gi`, `agent_min_replicas=1`, `agent_max_replicas=2`

Root Terraform exposes these knobs for controlled tuning without code changes:

- `agent_container_cpu`
- `agent_container_memory`
- `agent_min_replicas`
- `agent_max_replicas`

### Local Compose Defaults

The local `docker-compose.yml` sets an explicit runtime envelope for the `dlq-agent` service:

- Resource limits: `cpus=1.00`, `memory=2G`
- Resource reservations: `cpus=0.25`, `memory=512M`
- Queue/concurrency defaults:
   - `MAX_CONCURRENT_QUEUES=2`
   - `ASB_MAX_MESSAGE_COUNT=10`
   - `ASB_MAX_WAIT_TIME=5`
   - `PREFETCH_COUNT=20`
   - `AGENT_CYCLE_SLEEP_SECONDS=30`

### Queue Concurrency Tuning Guidance

Tune in this order to avoid lock churn and broker pressure:

1. Increase `MAX_CONCURRENT_QUEUES` only after CPU and memory remain stable at current load.
2. Increase `ASB_MAX_MESSAGE_COUNT` in small steps (for example: 10 -> 20) before increasing concurrency.
3. Keep `PREFETCH_COUNT` roughly `1x-2x` of `ASB_MAX_MESSAGE_COUNT`; avoid large prefetch under constrained memory.
4. Reduce `AGENT_CYCLE_SLEEP_SECONDS` only after confirming no sustained rise in `failure_queue_drain_total` and no lock-expiry symptoms.

### Expected Throughput and Safe Operating Envelope

Use these as planning ranges for deterministic triage-heavy workloads (not strict SLO guarantees):

- Safe baseline: `MAX_CONCURRENT_QUEUES=2-5`, `ASB_MAX_MESSAGE_COUNT=10`, `PREFETCH_COUNT=10-20`
- Caution band: `MAX_CONCURRENT_QUEUES=6-8` on 0.5 CPU/1Gi; monitor memory and queue drain failures closely
- Scale-out trigger: when backlog growth persists for 3+ cycles and CPU > 75% with stable failure rate, increase replica count before increasing per-replica concurrency

When AI fallback frequency is high, prioritise more CPU/memory per replica before increasing queue fan-out.

## 9. State Storage Strategy (Local vs Shared)

The agent currently uses local state backends:

- Idempotency: local `dbm` file (`IDEMPOTENCY_DB_PATH`)
- Classification cache: in-memory `TTLCache`

This model is valid for single-replica and low-to-moderate throughput operations, but it is not globally consistent across replicas.

### Decision Matrix

Keep local state when all are true:

1. `agent_max_replicas=1` in the target environment.
2. Duplicate suppression only needs per-instance scope.
3. Recovery can tolerate cache cold-start on restart.

Move to a shared state service (for example Redis) when any is true:

1. `agent_max_replicas > 1` for sustained operation.
2. Cross-replica idempotency guarantees are required.
3. Duplicate processing risk during failover is unacceptable.
4. Throughput requires horizontal scale beyond a single worker instance.

### Retention and Cleanup Policy

Idempotency retention:

- Controlled by `IDEMPOTENCY_TTL_SECONDS`.
- Expired keys are purged by the cleanup daemon.
- Cleanup cycle should remain aligned with load profile and storage growth.

Classification cache retention:

- Controlled by `CLASSIFICATION_TTL_SECONDS`.
- Capacity controlled by `CLASSIFICATION_CACHE_MAXSIZE`.
- Cache is process-local and intentionally ephemeral.

### Recovery Expectations

Current local-state recovery behaviour:

1. Container restart clears classification cache immediately.
2. Idempotency `dbm` persists while the mounted volume persists.
3. Volume replacement or loss resets idempotency history.

Operational implications:

1. After cold start, expect temporarily higher AI calls and lower cache-hit rate.
2. If idempotency volume is reset, expect short-lived duplicate reprocessing risk until TTL windows rebuild.

### Recommended Migration Sequence to Redis

1. Introduce a backend abstraction for idempotency and classification state.
2. Deploy Redis in private network scope with managed identity (preferred) or secret-backed auth.
3. Cut over idempotency first, then classification cache.
4. Validate parity using duplicate-drop and cache-hit metrics before increasing replica count.

## 10. Performance Baselines

Baseline numbers are generated locally with a repeatable benchmark harness:

```bash
make perf-baseline
```

Raw output is written to `reports/performance_baseline.json`.

### Current Baseline Snapshot (Representative Local Run)

Test profile:

- Batch sizes: `100`, `500`, `1000`
- AI fallback ratio: `20%`
- Cost assumptions: `900` input tokens + `120` output tokens per AI message, priced at `0.005` and `0.015` USD per 1k tokens respectively

Observed metrics:

- `100` messages: throughput `354.11 msg/s`, latency `p50 2.646ms`, `p95 3.159ms`, `p99 5.6ms`, estimated AI cost per batch `$0.126`
- `500` messages: throughput `389.24 msg/s`, latency `p50 2.438ms`, `p95 2.827ms`, `p99 5.214ms`, estimated AI cost per batch `$0.63`
- `1000` messages: throughput `380.58 msg/s`, latency `p50 2.504ms`, `p95 3.007ms`, `p99 5.24ms`, estimated AI cost per batch `$1.26`

### Notes and Interpretation

1. These are local synthetic baselines intended for trend comparison, not production SLA commitments.
2. Re-run after significant changes to classifier logic, retry/backoff behaviour, or AI provider configuration.
3. For model-specific cost accuracy, override:
   - `AI_EST_INPUT_TOKENS_PER_MESSAGE`
   - `AI_EST_OUTPUT_TOKENS_PER_MESSAGE`
   - `AI_PRICE_INPUT_PER_1K_USD`
   - `AI_PRICE_OUTPUT_PER_1K_USD`

## 11. Operator Dashboards and Runbook Automation

### Dashboard Query Pack

Use `docs/OPERATOR_DASHBOARD_QUERIES.md` as the source of truth for workbook tiles.

Included views:

1. Queue health
2. DLQ trend
3. Action distribution
4. Escalation rate

### Automated Triage Snapshot

Capture app status, active revisions, and recent logs to a timestamped report:

Direct script:

```bash
bash ./scripts/runbook_triage_snapshot.bash <resource-group> <container-app-name> [minutes]
```

Make shortcut:

```bash
make ops-triage-snapshot OPS_RESOURCE_GROUP=rg-dlq-msg-router-dev OPS_CONTAINER_APP=ca-dlq-msg-router-dev OPS_LOOKBACK_MINUTES=60
```

Output:

- `reports/triage/triage-<app>-<timestamp>.txt`

### Automated Rollback

Activate a previous container app revision:

Direct script:

```bash
bash ./scripts/runbook_rollback.bash <resource-group> <container-app-name> [target-revision]
```

If `target-revision` is omitted, the script selects the most recent inactive revision.

Make shortcut:

```bash
make ops-rollback OPS_RESOURCE_GROUP=rg-dlq-msg-router-dev OPS_CONTAINER_APP=ca-dlq-msg-router-dev OPS_TARGET_REVISION=<revision-name>
```

## 12. Multi-Namespace Deployment Patterns

The agent supports namespace target resolution in this precedence order:

1. `SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES` (comma-separated)
2. `SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE`
3. Namespace derived from `SERVICE_BUS_CONNECTION_STRING`

For multi-namespace mode, use managed identity and set:

```bash
SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES="ns-a.servicebus.windows.net,ns-b.servicebus.windows.net"
```

Do not combine `SERVICE_BUS_CONNECTION_STRING` with `SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES`.

### Pattern A: Separate Agent Per Namespace (Recommended Default)

Use this when:

1. Teams need strict blast-radius isolation.
2. Namespaces have different SLO or change windows.
3. You want per-namespace scaling and rollback independence.

Operationally:

1. Deploy one container app per namespace.
2. Set a single `SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE` per deployment.
3. Tune concurrency per namespace profile.

### Pattern B: Shared Cluster, Multi-Namespace Targets

Use this when:

1. Namespace traffic is moderate and operationally similar.
2. A single runbook and release train is preferred.
3. Cost pressure favours fewer app instances.

Operationally:

1. Use `SERVICE_BUS_FULLY_QUALIFIED_NAMESPACES`.
2. Keep `MAX_CONCURRENT_QUEUES` conservative initially and scale gradually.
3. Track per-queue metrics and escalation rates closely for noisy-neighbour effects.

### Selection Guidance

Choose separate agents when isolation and predictable rollback are top priorities.
Choose shared multi-namespace only when operational simplicity and cost are the primary drivers and throughput is within safe limits.