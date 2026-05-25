# Autonomous DLQ Smart Triage Router (Phase 2)

An intelligent, non-deterministic error resolution engine for Azure Service Bus. This system minimizes AI token consumption and maximizes operational governance by implementing a layered, defense-in-depth pipeline for Dead Letter Queue (DLQ) processing.

## 🏗️ Executive Summary

Enterprise messaging systems generate a long tail of unpredictable errors. Traditional approaches either rely on rigid, hardcoded routing that fails on novel anomalies, or they pipe every failure to an LLM, resulting in exorbitant costs and hallucination risks. 

This project implements a **Process Patterns, Not Messages** architecture. It acts as an automated triage pipeline, using deterministic heuristics for known business faults and relying on a strictly governed local LLM only for discovery and classification of unknown anomalies. 

### Core Optimizations
* **Zero-Touch Resolution:** Known structural errors incur $0 compute cost and zero LLM latency.
* **Dynamic Pattern Extraction:** Utilizes Regex capture groups to dynamically generate error fingerprints (e.g., `missing_field_email`), preventing rule-base bloat.
* **Human-in-the-Loop Governance:** The AI is locked in a read-only sandbox. It suggests classifications and detection rules, but messages are safely quarantined in a Parking Lot queue until human operators promote the rule.

---

## 🚦 The 5-Gate Triage Pipeline

Every PEEK_LOCKED message from the DLQ is evaluated through strict deterministic gates before invoking agentic capabilities:

1. **Gate A: Anti-Poison Pill:** Isolates message loops (Resubmit-Count >= 3) to prevent consumer crashing and infinite processing loops. Route: `Quarantined`.
2. **Gate B: Idempotency & Noise Suppression:** Cryptographically hashes the payload and correlation metadata. Suppresses redundant alerts for broken downstream clients spamming the broker. Route: `Dropped`.
3. **Gate C: Classification Cache:** Rapidly resolves recurring errors via a rolling in-memory cache (10-minute TTL) based on the error signature shape. Route: `Auto_Classified_From_Cache`.
4. **Gate D: Heuristic Router:** Evaluates Azure native metadata (`dead_letter_reason`, `error_description`) against dynamic JSON business rules. Route: `Auto_Classified`.
5. **Gate E: Agentic AI Fallback:** Unclassified anomalies trigger local LLM analysis to parse the raw payload, generating a JSON-structured triage contract. Route: `AI_Suggested_Rule_Pending_Approval`.

---

## 📂 Repository Architecture

```text
├── run_agent.py                            # Main orchestration and polling loop
├── scripts/
│   └── setup.sh                            # Idempotent environment initialization script
├── src/
│   ├── autonomous_dlq_classifier.py        # Core 5-gate pipeline and broker state management
│   ├── ai_client.py                        # LLM integration, payload truncation, and JSON salvage
│   ├── InMemoryCache.py                    # Thread-safe caching for idempotency and classifications
│   └── flush_queues.py                     # Utility to sterilize Azure queues for clean testing
├── simulator/
│   ├── producer.py                         # Generates synthetic enterprise payloads (Happy path + anomalies)
│   └── consumer.py                         # Simulates downstream rejections and native ASB timeouts
├── data/
│   └── rules.json                          # Extensible database of deterministic heuristic rules
├── tests/
│   └── test_autonomous_dlq_classifier.py   # Pytest suite for core business logic validation
├── .env.example                            # Configuration template
├── requirements.txt                        # Python dependencies
└── pytest.ini                              # Test configuration

```

---

## 🚀 Getting Started

### Prerequisites

* Python 3.10+
* Azure CLI authenticated (`az login`) with Azure Service Bus Data Owner RBAC.
* Ollama running locally (Default model: `llama3.2:latest`)

### 1. Environment Initialization

Run the initialization script to scaffold the virtual environment and install dependencies:

```bash
bash scripts/setup.sh
source .venv/bin/activate

```

### 2. Configuration

Copy the environment template and populate your Azure variables:

```bash
cp .env.example .env

```

Ensure `SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE`, `TARGET_QUEUE_NAME`, and `PARKING_LOT_QUEUE_NAME` are correctly defined.

---

## 🧪 Execution & Simulation

To observe the token-saving metrics and dynamic classification in real-time, execute the pipeline components sequentially across three terminal sessions.

**Terminal 1: Start the Autonomous Agent**

```bash
python run_agent.py

```

*The agent will begin polling the DLQ endpoint continuously.*

**Terminal 2: Start the Downstream Simulator (Consumer)**

```bash
python simulator/consumer.py

```

*Listens to the main queue, processing happy paths and explicitly rejecting simulated application/infrastructure faults to the DLQ.*

**Terminal 3: Fire the Synthetic Batch (Producer)**

```bash
python simulator/producer.py

```

*Dispatches a batch of 12 distinct event payloads into the architecture.*

### Observability

Upon execution, a `reports/telemetry_dashboard.csv` is generated dynamically. This ledger logs the timestamp, classification, specific pattern extracted, status, and the agent's confidence score for every message handled.

---

## 🛡️ Testing & Quality Assurance

The core business logic—fingerprint generation, cache TTL expiration, JSON salvage operations, and heuristic overrides—is fully decoupled from Azure infrastructure for rapid local testing.

Execute the test suite:

```bash
pytest
```

---

## 🔮 Strategic Roadmap (Phase 3)

As this architecture matures toward a multi-tenant enterprise deployment, the following enhancements are scoped for future iterations:

* **Pub/Sub Migration (TDLQ):** Transitioning from standard Queues to Azure Service Bus Topics and Subscriptions. This will require updating the agent to iterate over dynamically discovered Subscription Dead Letter Queues (TDLQ) rather than a single queue endpoint.
* **Claim-Check Pattern Integration:** For enterprise payloads exceeding Azure's size limits (e.g., >256KB or >1MB on Premium), the pipeline will be updated to handle Claim-Check tokens, automatically retrieving the heavy payload from Azure Blob Storage prior to AI analysis.
* **RAG-Powered Context Windowing:** Replacing the static `rules.json` injection with a Vector Database. The AI client will query top-K similar historical rules to provide context without exhausting the LLM's token window as the heuristic database scales.


