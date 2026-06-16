# Operational Runbook: DLQ Triage & Platform Dependencies

This guide details the Day 2 operational procedures for managing the Autonomous Dead Letter Queue (DLQ) agent. It covers telemetry extraction, synthetic traffic simulation, cloud cost controls, pipeline maintenance, and common troubleshooting patterns encountered in production.

## 1. Telemetry Dashboard & Log Analytics Extraction

Azure Container Apps (ACA) utilise ephemeral storage. Due to strict Role-Based Access Control (RBAC) mandates, operators are not granted the `Microsoft.App/containerApps/getAuthToken/action` permissions required to physically extract dashboard files (e.g., `telemetry_dashboard.csv`) via the Azure CLI.

**The Report-to-Log Pattern:**
To securely extract operational metrics without breaching least-privilege principles, the agent broadcasts structured CSV rows directly to standard output, prefixed with `CSV_EXPORT|`. 

To generate the operational dashboard, navigate to the Azure Log Analytics Workspace linked to the container environment and execute the following Kusto Query Language (KQL) script. Select 'Export to CSV' from the portal interface to download the results.

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-viva-dlq-agent-dev"
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

## 2. Platform Dependencies & Simulation (Local / Jumpbox)

If executing without live upstream traffic, operators must generate synthetic workloads to validate heuristic gates and AI fallback pathways.

### Setting up the ASB Simulators
Ensure you execute these commands from the repository root to avoid `ModuleNotFoundError` execution isolation traps.

1. **Sanitise the Environment:** Before initiating a test cycle, flush all ghost messages or orphaned payloads from the targeted Service Bus namespaces.

   ```bash
   python src/flush_queues.py
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

## 3. Cloud Cost Control & Infrastructure Teardown

When deployed to an Azure environment for testing or staging, operators must manage the infrastructure lifecycle to ensure zero cost overnight when pipeline testing is halted. 

### The Fixed-Cost Billing Reality
While the Azure Container Apps (ACA) compute layer can scale to zero, the underlying architecture relies on **Azure Service Bus (Premium Tier)** and **Azure Bastion**. These enterprise data plane services carry a substantial fixed hourly base cost regardless of active throughput or connected replicas. 

Manually scaling the Container App to zero via the Azure CLI **will not** stop the billing meters for the network and messaging infrastructure.

To successfully halt billing overnight, you must explicitly destroy the ephemeral compute and networking components via the Terraform orchestrator. This script surgically removes the expensive resources whilst preserving the Remote State (Storage Account) and Key Vault for rapid redeployment.

**Halt Billing (Destroy Ephemeral Infrastructure):**
Execute this from your local machine (not the Jumpbox):

    pwsh ./infra/scripts/99-destroy-all.ps1

**Resume Operations:**
Execute Phase 2 and Phase 4 from the Deployment Runbook to restore the network and agent.

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
**Root Cause:** The Terraform infrastructure provisioned a specific queue (e.g., `viva-payment-queue`), but local fallback JSON configurations (`data/asb_sources.json`) contain a typo (e.g., `payments` plural).
**Resolution:** Enable dynamic topology mapping by setting `ENABLE_DYNAMIC_DISCOVERY="True"` in the `.env` file. This forces the agent and simulators to query the `ServiceBusAdministrationClient` to programmatically discover the true state of the namespace, bypassing static JSON typos entirely.

### 3. AI Inference Causes AMQP Connection Drops (Local Dev Only)
**Symptom:** The local agent logs `amqp:connection:forced` followed by a cascade of `ValueError: Link already closed` errors.
**Root Cause:** When running locally against an offline LLM (Ollama), inference may consume 100% of the CPU, starving the Azure AMQP heartbeat thread. Azure detects the dropped heartbeat and severs the remote connection.
**Resolution:** Downgrade the local Ollama model to a lower parameter count (e.g., `qwen2.5:0.5b`) to reduce inference latency below the Azure timeout threshold. This issue does not occur in production as execution is routed to the external Azure AI Foundry endpoint.

## 7. Quality Gates & PII Handling

* **Formatting & Linting:** Run `black`, `isort`, and `pylint` (minimum score 8.0) across the `src/` directory before merging code.
* **Testing:** Run `pytest -v --cov=src` to validate core logic. The pipeline enforces an 85% coverage minimum.
* **PII Masking:** Before transmission to Azure Foundry, payloads are dynamically scrubbed. Phone numbers, emails, and valid Luhn-checksum credit cards are replaced with redacted placeholders. Standard 16-digit correlation IDs are preserved for telemetry debugging.
* **JSON Salvage:** To prevent pipeline-crashing `JSONDecode` errors resulting from LLM formatting hallucinations, the `ai_client.py` executes a defensive regex salvage protocol to strip unwanted markdown code blocks prior to parsing.