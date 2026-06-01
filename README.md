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

* **Local MVP (Current State):** Utilised via Docker Compose. State relies on local `dbm` and in-memory caches persisted via volume mounts. AI triage is executed via a locally hosted Ollama container to prevent token costs during testing. Emulators generate synthetic Azure Service Bus traffic.
* **Cloud Target (Future State):** Deployed as an Azure Container App. Will migrate caching to Azure Cache for Redis. The AI integration will point to Azure AI Foundry via managed identities to ensure enterprise-grade security and compliance boundary enforcement.

## Delivery Principles

- UK English documentation and naming conventions.
- AI is restricted to read-only analysis; it cannot autonomously modify message payloads or alter routing rules.
- Deterministic processing is prioritised to efficiently handle the vast majority of predictable, repeating errors, reserving AI compute for high-value anomaly detection.

## The 5-Gate Triage Architecture

To ensure strict payload security and deterministic routing, every Dead Letter message passes through an evaluation pipeline:

1. **Gate A: PII Scrubber & Poison Pill Quarantine** - Masks sensitive data (Luhn validation) and immediately quarantines messages exceeding the max delivery count.
2. **Gate B: Idempotency Store** - Prevents infinite loops by hashing message correlation IDs and dropping duplicates via a local cache.
3. **Gate C: Classification Cache** - Bypasses AI and heuristic engines for identical error shapes processed within the TTL window.
4. **Gate D: Heuristics Engine** - Evaluates messages against deterministic JSON rules. Supports queue-specific logic overrides.
5. **Gate E: AI Fallback** - Unknown anomalies are routed to the LLM for rule suggestion and quarantined in a human-review Parking Lot queue.

## Multi-Queue Configuration and Scalability

This application is scoped to run within a **single Azure tenant**. If processing requires isolation across multiple separate ASB namespaces (e.g., separating Premium tier traffic from Standard tier noisy neighbours), operational teams should deploy a separate container instance per namespace. 

Within a single namespace, the agent supports dynamic scaling across hundreds of Service Bus entities through the following operational models:

### 1. Small-Scale Deployments

Configuration is handled via a JSON array in the `.env` file:

```
ASB_SOURCES=[{"type": "queue", "name": "app-a-queue"}, {"type": "queue", "name": "app-b-queue"}] 
```

### 2. Large-Scale Deployments (600+ Queues)

Maintaining a hardcoded JSON array for hundreds of queues is an operational anti-pattern. This architecture addresses scale via:

* **Dynamic Discovery:** Utilises the `ServiceBusAdministrationClient` to programmatically query the namespace on boot, automatically discovering all eligible queues.
* **Exclusion Filters:** Utilises the `EXCLUDED_QUEUES` environment variable to blacklist specific topics or queues from the dynamic discovery process, isolating operational traffic.
* **Bounded Concurrency (Polling):** The agent does not assign a thread to every discovered DLQ simultaneously, which would cause resource exhaustion. Instead, it works through the discovered list using a fixed ThreadPool up to the `MAX_CONCURRENT_QUEUES` limit. Once the batch is processed, it initiates the next polling cycle. DLQs are not latency-sensitive, so this sequential polling ensures stability.
* **Event-Driven Limitations:** While Azure Event Grid can push notifications, it is restricted when interacting with private endpoint-secured resources and risks overwhelming the consumer if the grid drops events. Bounded polling guarantees reliable, persistent processing.

### Supported Execution Actions

* `drop`: Deletes the message silently (e.g., expired TTL).
* `drop_and_notify`: Deletes the message and alerts the upstream client.
* `retry`: Re-enqueues the message to the main queue (e.g., transient outages).
* `fix_and_retry`: Auto-heals structural payload issues via mapped safe defaults and re-enqueues.
* `escalate`: Routes to the parking lot queue for human review.

## Repository Layout

```
sc3-Autonomous-DLQ/
│
├── docs/                           
│   └── ops_guide.md                        # Runbook for workflows and dependencies
│
├── src/
│   ├── run_agent.py                        # Main orchestration and bounded polling loop
│   ├── autonomous_dlq_classifier.py        # Core 5-gate pipeline and broker state management
│   ├── action_executor.py                  # Command pattern implementations for DLQ actions
│   ├── ai_client.py                        # LLM Factory, payload truncation, and JSON salvage
│   └── state_managers.py                   # Thread-safe caching for idempotency and classifications
│
├── simulator/
│   ├── producer.py                         # Generates synthetic enterprise payloads
│   └── consumer.py                         # Simulates downstream rejections and native ASB timeouts
│
├── data/
│   └── rules.json                          # Database of deterministic heuristic rules
│
├── tests/                                  # Pytest suite for core business logic validation
├── Dockerfile                              # Multi-stage production container blueprint
├── docker-compose.yml                      # Local environment orchestration
├── requirements.txt                        # Production Python dependencies
└── requirements-dev.txt                    # CI/CD and linting dependencies
```

## Quick Start (Dockerized MVP)

### Preconditions & Operator Checklists

* [ ] Docker and Docker Compose installed.
* [ ] Azure CLI authenticated (`az login`) with Azure Service Bus Data Owner RBAC.
* [ ] A native local instance of Ollama running on the host machine (Default model: `llama3.2:latest`).

### 1. Environment Initialisation

Copy the environment template and populate your Azure variables:

```
cp .env.example .env
```

Ensure `SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE`, `ASB_SOURCES`, and `PARKING_LOT_QUEUE_NAME` are correctly defined.

### 2. Execution & Simulation

To observe the pipeline in real-time, execute the components sequentially across three terminal sessions from the root directory:

**Terminal 1: Start the Autonomous Agent (via Docker)**
This maps your local Azure Entra ID credentials into the container and links it to your host's Ollama instance.
```
docker-compose up --build -d
```

**Terminal 2: Start the Simulator (Consumer)**
```
python simulator/consumer.py
```

**Terminal 3: Fire the Synthetic Batch (Producer)**
```
python simulator/producer.py
```

Upon execution, a `reports/telemetry_dashboard.csv` is dynamically generated, logging the timestamp, classification, specific pattern extracted, status, and the agent's confidence score for every message handled.

## Strategic Roadmap

As this architecture matures toward a multi-queue enterprise deployment, the following enhancements are scoped for future iterations:

**Claim-Check Pattern Integration:** To maintain a lightweight parking lot queue, the message content from the DLQ will be persisted to Azure Blob Storage, with only a reference pointer placed on the parking lot queue.

**Taxonomy Decoupling:** Full extraction of the action glossary and classification definitions from the agent prompt into a remote configuration state to allow operational teams to manage definitions without modifying code.

**Advanced Telemetry Dashboarding:** Migration from the MVP CSV logging to Azure Log Analytics workspaces, visualised via PowerBI or Jupyter Notebooks to track rule hit rates, AI triage efficiency, and top DLQ-generating clients.