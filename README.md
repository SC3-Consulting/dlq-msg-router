# Viva DLQ Smart Triage Router (Phase 1 MVP)

An intelligent, token-optimized Dead Letter Queue (DLQ) processing engine built for Azure Service Bus. This MVP demonstrates a hybrid deterministic and agentic approach to handling enterprise integration failures.

## Architecture Overview

The system processes messaging failures using a defense-in-depth pipeline:
1. **The Heuristic Engine (Deterministic):** Evaluates incoming DLQ messages against a strict `rules.json` file. Known business logic errors (e.g., missing mandatory fields) are categorized instantly, incurring zero compute cost.
2. **The MD5 Fingerprinting & Cache:** Unhandled exceptions (like downstream Java gateway crashes) are stripped of dynamic memory allocation data and hashed. The resulting fingerprint is checked against a rolling in-memory cache to prevent redundant LLM invocations for cascading errors.
3. **The LLM Fallback (Agentic):** Net-new, unstructured errors are routed to a local LLM to extract root causes and suggest actionable fixes in a strict JSON contract.

## Project Structure

* `/scripts/producer.py` - Simulates the upstream system, emitting canonical data model payloads (Happy Path, Deterministic Flaws, and Poison Pills).
* `/scripts/consumer.py` - Simulates the downstream SAP environment. Handles native Azure Service Bus 3-strike retries and explicit dead-lettering.
* `/src/triage_agent.py` - The core orchestration engine watching the DLQ sub-queue.
* `/data/rules.json` - The local database of deterministic routing rules.

## Local Execution

### Prerequisites
* Azure CLI installed and authenticated (`az login`)
* Active Azure Service Bus Namespace (Basic Tier)
* Ollama running locally (Default: `llama-local`)

### Setup
1. Clone the repository and initialize the virtual environment:
   `python -m venv .venv`
   `source .venv/bin/activate`
2. Install dependencies:
   `pip install azure-identity azure-servicebus requests`
3. Update `FULLY_QUALIFIED_NAMESPACE` in the python scripts to match your Azure environment.

### Running the Pipeline
Execute the following sequentially to view the token-saving metrics ledger:
1. `python scripts/producer.py`
2. `python scripts/consumer.py`
3. `python src/triage_agent.py`