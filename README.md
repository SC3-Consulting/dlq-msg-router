# Viva DLQ Smart Triage Router (Phase 1 MVP)

An intelligent, token-optimized Dead Letter Queue (DLQ) processing engine built for Azure Service Bus. This MVP demonstrates a hybrid deterministic and agentic approach to handling enterprise integration failures.

## Architectural Pattern: "Process Patterns, Not Messages"

Enterprise messaging systems generate a long tail of unpredictable errors. Sending every failed message to an LLM is a waste of compute and tokens. This system uses a **Defense-in-Depth Pipeline** to ensure AI is only used as a last resort:

1. **The Heuristic Engine (Deterministic-First):** Evaluates incoming DLQ messages against a strict `rules.json` file. Known business logic errors (e.g., missing mandatory fields) are categorized instantly, incurring $0 compute cost.
2. **The MD5 Fingerprinting & Rolling Cache:** Unhandled exceptions (like downstream Java gateway crashes) are stripped of dynamic memory allocation data and timestamps. The resulting normalized string is hashed into a fingerprint. This fingerprint is checked against a rolling 10-minute in-memory cache to prevent redundant LLM invocations for cascading or duplicate errors.
3. **The LLM Fallback (Agentic):** Net-new, unstructured errors (Cache Misses) are routed to a local LLM to extract root causes and suggest actionable fixes following a strict JSON contract.

## Project Structure

* `/scripts/producer.py` - Simulates the upstream system (e.g., Salesforce), emitting canonical data model payloads including Happy Path, Deterministic Flaws, and Poison Pills.
* `/scripts/consumer.py` - Simulates the downstream environment (e.g., SAP). Handles native Azure Service Bus 3-strike retries and explicit dead-lettering.
* `/src/triage_agent.py` - The core orchestration engine watching the DLQ sub-queue, generating fingerprints, and routing logic.
* `/data/rules.json` - The local database of deterministic routing rules.
* `/tests/` - The `pytest` suite proving the core business logic (fingerprinting and caching).

## Core Schemas

### 1. The Canonical Input Payload
The pipeline expects messages in a standard enterprise event format. The producer generates these payloads:

```json
{
  "metadata": {
    "eventId": "evt-001",
    "correlationId": "corr-9901",
    "sourceSystem": "Salesforce",
    "eventType": "OrderCreated",
    "timestamp": "2026-05-18T10:00:00Z"
  },
  "payload": {
    "orderId": "ORD-77321",
    "clientId": "CLIENT_ACME",
    "transactionDate": "2026-05-18"
  }
}
```

### 2. The AI Classification Contract
When a message hits the AI Fallback, the LLM is strictly constrained to return this JSON schema. It is never allowed to modify the message itself; it only categorizes and suggests actions.

```json
{
  "classification": "string (e.g., DeadLetterQueueMessage)",
  "suggested_action": "string (e.g., Reprocess or Notify Client)",
  "confidence": 0.8
}
```

## System Outputs & Reporting

### The Triage Ledger (CSV Dashboard)
To fulfill the MVP requirement for data visibility, the Triage Agent automatically generates a persistent log of its routing decisions. When executed, a `reports/triage_ledger.csv` file is created/appended with the following schema:

| Timestamp | ClientID | Fingerprint | ResolutionType | Action |
| :--- | :--- | :--- | :--- | :--- |
| 2026-05-18T18:06:04Z | CLIENT_ACME | 1946f390 | `Heuristic_Match` | FIX_AND_RETRY |
| 2026-05-18T18:06:36Z | TRIGGER_SAP | dde7cb33 | `AI_Cache_Miss` | Re-queue message |
| 2026-05-18T18:06:37Z | TRIGGER_SAP | dde7cb33 | `AI_Cache_Hit` | Re-queue message |

Notice how the second `TRIGGER_SAP` error generates an `AI_Cache_Hit` for the exact same fingerprint, proving the deduplication engine bypassed the LLM.

## Local Execution Guide

### Prerequisites
* Azure CLI installed and authenticated (`az login`)
* Active Azure Service Bus Namespace (Basic Tier) with Azure Service Bus Data Owner RBAC assigned to your identity
* Ollama running locally (Default model: `llama-local`)
* Python 3.10+

### Setup
Clone the repository and initialize the virtual environment:

```bash
python -m venv .venv
source .venv/bin/activate
```

Install dependencies:

```bash
pip install azure-identity azure-servicebus requests pytest
```

Update `FULLY_QUALIFIED_NAMESPACE` in the python scripts to match your Azure environment.

### Running the Live Fire Pipeline
Execute the following sequentially to view the token-saving metrics in action:

```bash
python scripts/producer.py
python scripts/consumer.py
python src/triage_agent.py
```

### Running the Test Suite
The business logic (Fingerprint normalization, Cache hits, Cache expiration, and Heuristic overrides) is fully tested. To run the suite without hitting Azure or Ollama:

```bash
pytest tests/
```
