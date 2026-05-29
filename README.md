# Autonomous DLQ Triage Pipeline

This repository provisions and operates a non-deterministic error resolution engine for Azure Service Bus Dead Letter Queues (DLQ).

## Scope

- Implements a "Process Patterns, Not Messages" principle to handle message failures at scale.
- Natively supports both Queue DLQs and Topic Subscription DLQs via dynamic JSON configuration mapping.
- Utilises deterministic heuristics for known business faults to minimise compute latency and token consumption.
- Employs a strictly governed LLM fallback solely for the discovery, classification, and clustering of unknown anomalies.
- Supports local LLM providers (e.g., Ollama) for the initial PoC phase, with an extensible design targeting Azure AI Foundry OpenAI Service for production state.
- Designed for highly sensitive message payloads (e.g., financial transactions) where fully agentic message modification and routing are deemed too unpredictable and risky.

## Local Development vs Cloud Target

To maintain a clear segregation between the MVP and the production architecture, the environment dependencies are strictly bifurcated:

* **Local MVP (Current State):** Utilises local `dbm` and in-memory caches. AI triage is executed via a locally hosted Ollama container (e.g., `llama3.2:latest`) to prevent token costs during testing. Emulators generate synthetic Azure Service Bus traffic.
* **Cloud Target (Future State):** Will migrate caching to Azure Cache for Redis. The AI integration will point to Azure AI Foundry (OpenAI Service) via managed identities to ensure enterprise-grade security and compliance boundary enforcement.

## Delivery Principles

- UK English documentation and naming conventions.
- AI is restricted to read-only analysis; it cannot autonomously modify message payloads or alter routing rules.
- Deterministic processing is prioritised to efficiently handle the vast majority of predictable, repeating errors, reserving AI compute for high-value anomaly detection.

## The 5-Gate Triage Architecture
To ensure zero-trust security and deterministic routing, every Dead Letter message passes through a strict evaluation pipeline:

1. **Gate A: PII Scrubber & Poison Pill Quarantine** - Masks sensitive data (Luhn validation) and immediately quarantines messages exceeding the max delivery count.
2. **Gate B: Idempotency Store** - Prevents infinite loops by hashing message correlation IDs and dropping duplicates via a local `dbm` cache.
3. **Gate C: Classification Cache** - Bypasses AI and heuristic engines for identical error shapes processed within the TTL window.
4. **Gate D: Heuristics Engine (Multi-Tenant)** - Evaluates messages against deterministic JSON rules. Supports tenant-specific overrides based on the origin queue.
5. **Gate E: AI Fallback** - Unknown anomalies are routed to a local LLM for rule suggestion and quarantined in a human-review Parking Lot queue.

## ⚙️ Multi-Tenant Configuration
The agent supports dynamic scaling across multiple Service Bus entities without duplicating deployments. Configuration is handled via a JSON array in the `.env` file:

```env
ASB_SOURCES=[{"type": "queue", "name": "tenant-a-queue"}, {"type": "queue", "name": "tenant-b-queue"}] 
```
Each source initialises its own asynchronous processing thread while sharing the centralized Idempotency Store and Classification Cache.

### Scaling to 600+ Queues (Target State):
Maintaining a hardcoded JSON array for hundreds of queues is an operational anti-pattern that leads to .env file bloat and requires manual deployment updates for every new tenant. In the production target state, the ASB_SOURCES variable will be deprecated in favor of:

1. **Dynamic Polling (Pull):** Utilising ServiceBusAdministrationClient to programmatically query the namespace on boot, list all queues, and dynamically assign threads only to queues where dead_letter_message_count > 0.

2. **Event-Driven (Push):** Utilising Azure Event Grid to detect DLQ messages globally and trigger a serverless Azure Function orchestrator, eliminating idle polling entirely.

### Supported Execution Actions
* `drop`: Deletes the message silently (e.g., expired TTL).
* `drop_and_notify`: Deletes the message and alerts the upstream client.
* `retry`: Re-enqueues the message to the main queue (e.g., transient outages).
* `fix_and_retry`: Auto-heals structural payload issues and re-enqueues.
* `escalate`: Routes to the parking lot queue for human review.

## Repository Layout

```text
sc3-Autonomous-DLQ/
│
├── docs/                           
│   └── ops_guide.md                        # Runbook for parking lot workflows and dependencies
│
├── src/
│   ├── run_agent.py                        # Main orchestration and polling loop
│   ├── autonomous_dlq_classifier.py        # Core 5-gate pipeline and broker state management
│   ├── action_executor.py                  # Command pattern implementations for DLQ actions
│   ├── ai_client.py                        # LLM integration, payload truncation, and JSON salvage
│   └── state_managers.py                   # Thread-safe caching for idempotency and classifications
│
├── simulator/
│   ├── producer.py                         # Generates synthetic enterprise payloads
│   └── consumer.py                         # Simulates downstream rejections and native ASB timeouts
│
├── data/
│   └── rules.json                          # Multi-tenant database of deterministic heuristic rules
│
├── scripts/
│   └── setup.sh                            # Idempotent environment initialisation script
│
├── tests/                                  # Pytest suite for core business logic validation
├── .env.example                            # Configuration template
└── requirements.txt                        # Python dependencies
```

## Quick Start

### Preconditions & Operator Checklists

* [ ] Python 3.10+
* [ ] Azure CLI authenticated (`az login`) with Azure Service Bus Data Owner RBAC.
* [ ] Ollama running locally (Default model: `llama3.2:latest`)

### 1. Environment Initialisation
Run the initialisation script to scaffold the virtual environment and install dependencies:

```bash
bash scripts/setup.sh
source .venv/bin/activate
```
### 2. Configuration
Copy the environment template and populate your Azure variables:

```bash
cp .env.example .env
```
Ensure SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE, ASB_SOURCES, and PARKING_LOT_QUEUE_NAME are correctly defined.

Operators should also tune the polling limits (ASB_MAX_MESSAGE_COUNT, ASB_MAX_WAIT_TIME) and cache TTLs (IDEMPOTENCY_TTL_SECONDS, CLASSIFICATION_TTL_SECONDS) based on expected traffic volumes.

### 3. Execution & Simulation
To observe the pipeline in real-time, execute the components sequentially across three terminal sessions from the root directory:

**Terminal 1: Start the Autonomous Agent**

```bash
python -m src.run_agent
```
**Terminal 2: Start the Simulator (Consumer)**

```bash
python simulator/consumer.py
```
**Terminal 3: Fire the Synthetic Batch (Producer)**

```bash
python simulator/producer.py
```
Upon execution, a reports/telemetry_dashboard.csv is dynamically generated, logging the timestamp, classification, specific pattern extracted, status, and the agent's confidence score for every message handled.

## Strategic Roadmap
As this architecture matures toward a multi-tenant enterprise deployment, the following enhancements are scoped for future iterations:

**Claim-Check Pattern Integration:** To maintain a lightweight parking lot queue, the message content from the DLQ will be persisted to Azure Blob Storage, with only a reference pointer placed on the parking lot queue.

**Taxonomy Decoupling:** Full extraction of the action glossary and classification definitions from the agent prompt into a remote configuration state to allow operational teams to manage definitions without modifying code.

**Advanced Telemetry Dashboarding:** Migration from the MVP CSV logging to Azure Log Analytics workspaces, visualised via PowerBI or Jupyter Notebooks to track rule hit rates, AI triage efficiency, and top DLQ-generating clients.