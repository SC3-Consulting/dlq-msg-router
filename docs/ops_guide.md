# Operational Runbook: DLQ Triage & Platform Dependencies

This guide details the operational procedures for setting up platform dependencies, managing Dead Letter Queue (DLQ) anomalies, processing suspended messages, and maintaining code quality.

## 1. Broader Dependencies Setup

Before running the agent, the following infrastructure components must be configured.

### Setting up Ollama (AI Engine)

The local MVP relies on Ollama for LLM inference to avoid frontier model token costs during initial triage.

1. Install Ollama from the official website.
2. Start the background service: `ollama serve`
3. Pull the required model: `ollama pull llama3.2:latest` (Default classifier).
4. Verify the endpoint is accessible. When using Docker Compose, the container utilises the `host.docker.internal` bridge. Ensure `OLLAMA_ENDPOINT` in your `.env` file reflects this architecture (e.g., `http://host.docker.internal:11434/api/generate`).

### Setting up the ASB Emulator (Simulators)

If you do not have live enterprise traffic, you must generate synthetic workloads using the provided emulation scripts.

1. Ensure your Azure Service Bus namespace and queues are provisioned.
2. Authenticate your CLI session: `az login`
3. Start the **Consumer Emulator** (`python simulator/consumer.py`) to intentionally reject specific messages and simulate downstream outages.
4. Start the **Producer Emulator** (`python simulator/producer.py`) to dispatch a batch of specific test payloads designed to trigger the deterministic gates and AI fallback pathways.

## 2. The Human-in-the-Loop Concept

The Autonomous DLQ Agent is strictly governed. When a novel error arrives that does not match the deterministic `data/rules.json` configurations, the AI Agent will analyse the payload. 

Crucially, **the AI is not permitted to alter the message or execute a destructive action.** It will route the message to the `parking-lot-queue` and log a suggested rule. Human operators must review the telemetry, approve or reject the AI's suggestion, and manually update the rules engine.

## 3. Parking Lot Remediation Workflow

When an unclassified message is assigned the status `AI_Suggested_Rule_Pending_Approval`, execute the following workflow:

### Step 1: Assess the AI Recommendation

Review the telemetry dashboard (`reports/telemetry_dashboard.csv`) or Log Analytics Workspace. Identify the suggested classification, pattern, and action provided by the AI.

### Step 2: Approve or Reject

* **Reject:** If the AI hallucinated or the error is a once-off glitch, no system changes are required. The message in the parking lot can be dropped or manually retried via Azure Service Bus Explorer.
* **Approve:** If the error is a valid, repeating structural fault, proceed to Step 3.

### Step 3: Update the Heuristics Engine

Do not permit AI to write code directly. A platform engineer must codify the AI's recommendation into `data/rules.json`. Decide if the rule applies globally or to a specific queue.

*Example of a newly codified AI rule:*
```
{
  "rule_id": "app_005",
  "severity_score": 40,
  "classification": "Schema_Validation_Failed",
  "pattern_name": "missing_transaction_date",
  "condition": "reason == 'ValidationFailed' and client_id == 'Alpha_Corp'",
  "pattern_regex": "missing mandatory field: 'transaction_date'",
  "default_action": "fix_and_retry",
  "safe_defaults_map": {
    "transaction_date": "1970-01-01T00:00:00Z"
  }
}
```

### Step 4: Reloading the Rules

The agent caches the heuristic rules in memory at startup. To apply the new `rules.json` modifications:

1. Send a `SIGINT` (Ctrl+C) to the Docker container or orchestrator process (`docker-compose stop dlq-agent`).
2. The agent will gracefully finish processing its current ASB batch to prevent orphaned locks, execute explicit AMQP socket closure, and then terminate.
3. Restart the agent (`docker-compose start dlq-agent`). It will load the updated JSON and automatically flush the in-memory cache.

### Step 5: Process the Parking Lot

Currently, the Parking Lot requires manual intervention via Azure Service Bus Explorer (or a claim-check processing script in Target State). 
Select the pending messages in the `parking-lot-queue` and resubmit them to the original upstream queue. 
Because the Heuristics Engine has now been updated, the Agent will immediately detect them on their next failure and automatically apply the new deterministic action (e.g., `fix_and_retry`), reducing the AI token cost.

## 4. State Persistence & Ephemeral Container Storage

Container local storage is ephemeral. If the container restarts, internal files are wiped.

* The `docker-compose.yml` mounts local volumes for `./data` and `./reports`. This ensures the `IdempotencyStore` (`dbm`) and telemetry logs survive container restarts.
* In a production Azure Container App deployment, the `IdempotencyStore` must be migrated to Azure Cache for Redis to prevent silent data loss during scale-out operations.

## 5. Code Quality & CI/CD Gates

To maintain enterprise engineering standards, this repository enforces strict quality gates. Before committing code changes or merging rule updates, engineers must validate the pipeline using `requirements-dev.txt`:

* **Formatting:** Run `black src/ simulator/ tests/` and `isort src/ simulator/ tests/` to enforce PEP-8 compliance.
* **Linting:** Run `pylint src/ --fail-under=8` to identify code smells, anti-patterns, and unused variables. The pipeline will reject code dropping below an 8.0 score.
* **Static Type Checking:** Run `mypy src/` to ensure type safety across the execution modules.
* **Testing & Coverage:** Run `pytest -v --cov=src --cov-fail-under=85 tests/` to execute the isolated unit tests. Commits will fail if test coverage drops below 85%.

## 6. Sensitive Data Handling (PII Masking)

Before any payload is transmitted to the AI engine for triage or logged in telemetry, sensitive information is dynamically masked:

* **Emails / Phones:** Replaced with redacted placeholder tags.
* **Credit Cards:** Evaluated against the mathematical Luhn algorithm checksum. Valid credit card numbers are redacted, whilst standard 16-digit system correlation IDs are safely preserved for debugging.

## 7. Scalability & Thread Management

The orchestrator utilises a bounded concurrency model to handle hundreds of queues without resource exhaustion.

**Architectural Constraints:**

1. **Connection Multiplexing:** The agent leverages a `ServiceBusClient` Singleton Factory to multiplex a single underlying AMQP network connection across all active threads. The Azure Service Bus Python SDK is not inherently thread-safe; therefore, thread locks are implemented within the factory to safely vend receivers/senders.
2. **Bounded Thread Pool:** The agent does not assign a thread per DLQ (e.g., 600 queues = 600 threads). This is unscalable. Instead, it defines a maximum concurrency limit (`MAX_CONCURRENT_QUEUES`).
3. **Sequential Polling Strategy:** The pool works through the list of discovered queues up to the concurrency limit. Once the batch is complete, the agent yields the CPU via a configurable sleep cycle before scanning the namespaces again. Because DLQs do not require real-time latency SLAs, this approach guarantees high throughput with low resource utilisation.
4. **Multi-Instance Pattern Matching:** If scaling limitations require deploying multiple agent instances within the same namespace, dynamic discovery must be augmented with name pattern matching (e.g., Instance A polls *payments*, Instance B polls *integration*) to prevent duplicated processing across instances.

## Troubleshooting & Known Failure States

### 1. AI Inference Causes AMQP Connection Drops

**Symptom:** The agent logs `amqp:connection:forced` followed by a cascade of `ValueError: Link already closed` errors.
**Root Cause:** The local LLM (Ollama) is consuming 100% of the CPU, starving the Azure AMQP thread. Azure detects no network heartbeats and forcefully severs the connection for 4 minutes (240,000ms).
**Resolution:** 
1. Ensure the Azure Queue Lock Duration is set to **5 minutes**. 
2. Downgrade the local Ollama model to a lower parameter count (e.g., `qwen2.5:0.5b`) to reduce CPU inference time below the Azure timeout threshold.
3. Target State: Migrate the AI fallback to the Azure AI Foundry cloud endpoint.

### 2. Ghost Messages Resurfacing

**Symptom:** Messages that were supposed to be dropped are continually being re-processed on startup.
**Root Cause:** The `IdempotencyStore` has cached stale correlation IDs from previous failed test runs.
**Resolution:** Wipe the local database and flush the broker queues to reset the pipeline state:

```
rm -rf data/idempotency.db*
python simulator/flush_queues.py
```