# Autonomous DLQ Triage Pipeline

This repository provisions and operates a non-deterministic error resolution engine for Azure Service Bus Dead Letter Queues (DLQ).

## Scope

- Implements a "Process Patterns, Not Messages" principle to handle message failures at scale.
- Natively supports Queue DLQs via dynamic configuration mapping and discovery.
- Utilises deterministic heuristics for known business faults to minimise compute latency and token consumption.
- Employs a strictly governed LLM fallback solely for the discovery, classification, and clustering of unknown anomalies.
- Utilises Azure AI Foundry OpenAI Service via user-assigned managed identities for the production state, with fallback support for local LLM providers (e.g., Ollama) during offline development.
- Designed for highly sensitive message payloads (e.g., financial transactions) where fully agentic message modification and routing are deemed too unpredictable and risky.

## Deployment Architecture

To maintain a clear segregation between offline development and the production architecture, the environment dependencies are strictly bifurcated:

* **Cloud Target (Current State):** Deployed as an immutable container instance via Azure Container Apps (ACA). Pulls images from Azure Container Registry (ACR). Authenticates to Azure Service Bus and Azure AI Foundry passwordless via a User-Assigned Managed Identity. 
* **Local Development:** Utilised via Docker Compose. State relies on local `dbm` and in-memory caches persisted via volume mounts. AI triage is executed via a locally hosted Ollama container to prevent token costs during offline testing. Emulators generate synthetic Azure Service Bus traffic.

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
5. **Gate E: AI Fallback** - Unknown anomalies are routed to the Azure AI Foundry model for rule suggestion and quarantined in a human-review Parking Lot queue.

## Multi-Queue Configuration and Scalability

This application is scoped to run within a **single Azure tenant**. If processing requires isolation across multiple separate ASB namespaces (e.g., separating Premium tier traffic from Standard tier noisy neighbours), operational teams should deploy a separate container instance per namespace. 

Within a single namespace, the agent supports dynamic scaling across hundreds of Service Bus entities through the following operational models:

### 1. Static/Fallback Configuration

Configuration is handled via a dedicated JSON file referenced in the `.env` file. This acts as the primary source for small-scale deployments, or as a safety fallback if dynamic discovery is explicitly disabled or hits an RBAC 403 error.

```bash
# .env
ASB_SOURCES_FILE="data/asb_sources.json"
```

```json
// data/asb_sources.json
[
  {"type": "queue", "name": "app-a-queue"},
  {"type": "queue", "name": "app-b-queue"}
]
```

### 2. Large-Scale Deployments

Maintaining a hardcoded JSON array for hundreds of queues is an operational anti-pattern. This architecture addresses scale via:

* **Dynamic Discovery:** Utilises the `ServiceBusAdministrationClient` to programmatically query the namespace on boot, automatically discovering all eligible queues.
* **Exclusion Filters:** Utilises the `EXCLUDED_QUEUES` environment variable to blacklist specific topics or queues from the dynamic discovery process, isolating operational traffic.
* **Bounded Concurrency (Polling):** The agent does not assign a thread to every discovered DLQ simultaneously, which would cause resource exhaustion. Instead, it works through the discovered list using a fixed ThreadPool up to the `MAX_CONCURRENT_QUEUES` limit. Once the batch is processed, it initiates the next polling cycle. DLQs are not latency-sensitive, so this sequential polling ensures stability.

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
* [ ] (Optional) A native local instance of Ollama running on the host machine for offline execution.

### 1. Environment Initialisation

Copy the environment template and populate your Azure variables:

```bash
cp .env.example .env
```

Ensure `SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE`, `ASB_SOURCES`, and `PARKING_LOT_QUEUE_NAME` are correctly defined.

### 2. Execution & Simulation (Local)

To observe the pipeline in real-time, execute the components sequentially across three terminal sessions from the root directory:

**Terminal 1: Start the Autonomous Agent (via Docker)**
This maps your local Azure Entra ID credentials into the container.
```bash
docker-compose up --build -d
```

**Terminal 2: Start the Simulator (Consumer)**
```bash
python simulator/consumer.py
```

**Terminal 3: Fire the Synthetic Batch (Producer)**
```bash
python simulator/producer.py
```

Upon execution, a `reports/telemetry_dashboard.csv` is dynamically generated, logging the timestamp, classification, specific pattern extracted, status, and the agent's confidence score for every message handled.

## Strategic Roadmap

As this architecture matures toward a multi-queue enterprise deployment, the following enhancements are scoped for future iterations:

**Infrastructure as Code (IaC):** Migration to a fully automated Terraform deployment within a secured private virtual network (VNet).

**Claim-Check Pattern Integration:** To maintain a lightweight parking lot queue, the message content from the DLQ will be persisted to Azure Blob Storage, with only a reference pointer placed on the parking lot queue.

**Taxonomy Decoupling:** Full extraction of the action glossary and classification definitions from the agent prompt into a remote configuration state to allow operational teams to manage definitions without modifying code.