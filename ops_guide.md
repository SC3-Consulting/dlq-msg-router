# Operational Runbook: DLQ Triage & Platform Dependencies

This guide details the operational procedures for setting up platform dependencies, managing Dead Letter Queue (DLQ) anomalies, processing suspended messages, and maintaining code quality.

## 1. Broader Dependencies Setup

Before running the agent, the following local infrastructure components must be configured.

### Setting up Ollama (AI Engine)
The agent relies on Ollama for local LLM inference to avoid frontier model token costs during initial triage.
1. Install Ollama from the [official website](https://ollama.com).
2. Start the background service: `ollama serve`
3. Pull the required model:
   * `ollama pull llama3.2:latest` (Default classifier)
4. Verify the endpoint is accessible at `http://localhost:11434`. This URL must be mapped to `OLLAMA_ENDPOINT` in your `.env` file.

### Setting up the ASB Emulator (Simulators)
If you do not have live enterprise traffic, you must generate synthetic workloads using the provided emulation scripts.
1. Ensure your Azure Service Bus namespace and queues are provisioned.
2. Authenticate your CLI session: `az login`
3. Start the **Consumer Emulator** (`python simulator/consumer.py`). This script listens to the target queue and intentionally rejects specific messages to simulate upstream service outages and application faults.
4. Start the **Producer Emulator** (`python simulator/producer.py`). This script dispatches a batch of specific test payloads (poison pills, schema violations) designed to trigger the deterministic gates and AI fallback pathways.

## 2. The Human-in-the-Loop Concept

The Autonomous DLQ Agent is strictly governed. When a novel error arrives that does not match the deterministic `data/rules.json` configurations, the AI Agent will analyse the payload. 

Crucially, **the AI is not permitted to alter the message or execute a destructive action.** It will route the message to the `parking-lot-queue` and log a suggested rule. Human operators must review the telemetry, approve or reject the AI's suggestion, and manually update the rules engine.

## 3. Parking Lot Remediation Workflow

When an unclassified message is assigned the status `AI_Suggested_Rule_Pending_Approval`, execute the following workflow:

### Step 1: Assess the AI Recommendation
Review the telemetry dashboard (`reports/telemetry_dashboard.csv`) or your log aggregation tool (e.g., Log Analytics Workspace).
Identify the suggested classification, pattern, and action provided by the AI.

### Step 2: Approve or Reject
* **Reject:** If the AI hallucinated or the error is a once-off glitch, no system changes are required. The message in the parking lot can be dropped or manually retried using Azure Service Bus Explorer.
* **Approve:** If the error is a valid, repeating structural fault, proceed to Step 3.

### Step 3: Update the Heuristics Engine
Do not permit AI to write code directly. A platform engineer must codify the AI's recommendation into `data/rules.json`.
1. Open `data/rules.json`.
2. Add the new rule block.
3. Decide if the rule applies globally (add to `global_rules`) or to a specific tenant/queue (add to `queue_overrides`).

*Example of a newly codified AI rule:*
```json
{
  "rule_id": "app_005",
  "severity_score": 40,
  "classification": "Schema_Validation_Failed",
  "pattern_name": "missing_transaction_date",
  "condition": "reason == 'ValidationFailed' and client_id == 'Alpha_Corp'",
  "pattern_regex": "missing mandatory field: 'transaction_date'",
  "default_action": "fix_and_retry"
}
```
### Step 4: Reloading the Rules
The agent caches the heuristic rules in memory at startup. To apply the new `rules.json` modifications:
1. Send a `SIGINT` (Ctrl+C) to the `run_agent.py` process. The agent will gracefully finish processing its current ASB batch to prevent orphaned locks.
2. Restart the agent (`python -m src.run_agent`). 
3. During boot, the agent will load the updated JSON and automatically flush the in-memory `ClassificationCache` to ensure state consistency.

### Step 5: Process the Parking Lot
Currently, the Parking Lot requires manual intervention via Azure Service Bus Explorer (or a claim-check processing script in Target State). 
Select the pending messages in the `parking-lot-queue` and resubmit them to the original upstream queue. 
Because the Heuristics Engine has now been updated, the Agent will immediately detect them on their next failure and automatically apply the new deterministic action (e.g., `fix_and_retry`), reducing the AI token cost.

## 4. State Persistence & Ephemeral Container Storage

The MVP utilises Python's native `dbm` for the `IdempotencyStore` to track and suppress duplicate message processing loops, and `cachetools` for the in-memory `ClassificationCache`. 

**Critical Operational Constraint:** If deploying this agent as a Docker container (e.g., via Azure Container Apps), local filesystem and memory storage are ephemeral. 
* **Idempotency Store (`dbm`):** Will not survive a container restart or crash, resetting the duplicate counters.
* **Classification Cache (`cachetools`):** Will also reset. As per design, it does not matter too much if this in-memory TTL cache resets upon restart, as it will rapidly rebuild from the deterministic rules engine.

**Target State Recommendations:**
* **Immediate Mitigation:** Map the `IDEMPOTENCY_DB_PATH` environment variable to a persistent external volume mount attached to the container.
* **Production Architecture:** Replace the local `dbm` implementation with a distributed, persistent key-value store such as Azure Cache for Redis or Azure Cosmos DB.

## 5. Code Quality & CI/CD Gates

To maintain enterprise engineering standards, this repository is equipped with strict quality gates. Before committing code changes or merging rule updates, operators and engineers must validate the pipeline using the provided tools:

* **Formatting:** Run `black src/ simulator/ tests/` and `isort src/ simulator/ tests/` to enforce PEP-8 compliance and resolve import sorting.
* **Linting:** Run `pylint src/` to identify code smells, anti-patterns, and unused variables.
* **Static Type Checking:** Run `mypy src/` to ensure type safety across the execution modules.
* **Testing & Coverage:** Run `pytest -v --cov=src tests/` to execute the isolated unit tests (validating deterministic gates, caching, and multi-tenant rule overrides) and verify code coverage percentages.

## 6. Zero Trust Data Handling (PII Masking)

To comply with enterprise security standards, the agent includes an automated `PIIScrubber`. 

Before any payload is transmitted to the AI engine for triage or logged in the telemetry dashboard, sensitive information is dynamically masked:
* **Emails:** Replaced with `[REDACTED_EMAIL]`.
* **Phone Numbers:** Replaced with `[REDACTED_PHONE]`.
* **Credit Cards:** Evaluated against the mathematical Luhn algorithm checksum. Valid credit card numbers are replaced with `[REDACTED_CC]`, whilst standard 16-digit system correlation IDs are safely preserved for debugging.

## 7. Scalability Limits (Handling 600+ Queues)

The current MVP uses the `ASB_SOURCES` JSON array in the `.env` file to spin up parallel processing threads for each tenant DLQ. 

**Operational Warning:** Do not attempt to stack hundreds of queues into this `.env` array. 
Doing so will:
1. Make the environment variable unreadable and prone to syntax errors.
2. Exhaust the container's thread pool and CPU limits, as the agent attempts to hold hundreds of concurrent AMQP network connections open simultaneously.

**Scaling Protocol:** Until the Azure Event Grid (Push) or Auto-Discovery (Pull) target state architecture is deployed, operators must shard the workloads. If managing a large namespace, deploy multiple instances of the Autonomous Agent container, assigning a maximum of 10-20 queues per `.env` file array per container instance to distribute the network I/O and compute load.

## Troubleshooting & Known Failure States

### 1. AI Inference Causes AMQP Connection Drops
**Symptom:** The agent logs `amqp:connection:forced` followed by a cascade of `ValueError: Link already closed` errors.
**Root Cause:** The local LLM (Ollama) is consuming 100% of the CPU, starving the Azure AMQP thread. Azure detects no network heartbeats for 4 minutes (240,000ms) and forcefully severs the connection.
**Resolution:** 1. Ensure the Azure Queue Lock Duration is set to **5 minutes**.
2. Downgrade the local Ollama model to a lower parameter count (e.g., `qwen2.5:0.5b`) to reduce CPU inference time below the Azure timeout threshold.
3. Target State: Migrate the AI fallback to the Azure AI Foundry cloud endpoint.

### 2. Ghost Messages Resurfacing
**Symptom:** Messages that were supposed to be dropped are continually being re-processed on startup.
**Root Cause:** The local Idempotency Store (`data/idempotency.db`) has corrupted or cached stale correlation IDs from previous failed test runs.
**Resolution:** Wipe the local database and flush the broker queues to reset the pipeline state:
```bash
rm -rf data/idempotency.db*
python src/flush_queues.py