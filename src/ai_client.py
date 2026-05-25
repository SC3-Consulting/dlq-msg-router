import json
import logging
import os
import requests

class LocalOllamaClient:
    def __init__(self):
        # Strict .env enforcement
        self.model_name = os.getenv("OLLAMA_MODEL")
        self.base_url = os.getenv("OLLAMA_ENDPOINT")
        self.logger = logging.getLogger("LocalOllamaClient")
        
        if not self.model_name or not self.base_url:
            raise ValueError("Missing Ollama configurations in .env")

        self.valid_actions = [
            "drop", 
            "drop_and_notify", 
            "retry", 
            "fix_and_retry", 
            "escalate", 
            "classify"
        ]

    def call_llm(self, client_id, reason, description, payload):
        """Executes the AI classification using the local Ollama API."""
        
        # FIX: Protect Context Window from massive payloads
        safe_payload = payload
        if len(payload) > 3000:
            self.logger.warning(f"Payload too large for AI context. Truncating for client {client_id}.")
            safe_payload = payload[:1500] + "\n... [TRUNCATED] ...\n" + payload[-1500:]

        prompt = self._build_prompt(client_id, reason, description, safe_payload)
        
        request_payload = {
            "model": self.model_name,
            "prompt": prompt,
            "stream": False,
            "options": {
                "temperature": 0.1, 
                "num_ctx": 4096     
            }
        }

        self.logger.info(f"Invoking Ollama model: {self.model_name} for client: {client_id}")
        
        try:
            response = requests.post(self.base_url, json=request_payload, timeout=120)
            response.raise_for_status()
            return response.json().get("response", "{}")
            
        except requests.exceptions.RequestException as e:
            self.logger.error(f"Ollama API failure: {e}")
            raise

    def _build_prompt(self, client_id, reason, description, payload):
        return f"""
You are an operations support engineer managing an Azure Service Bus environment.
A message has failed the deterministic heuristic checks and fallen into the Dead Letter Queue (DLQ).
Analyze the raw payload to deduce why it failed and recommend how to handle it.

--- MESSAGE CONTEXT ---
Client ID: {client_id}
System DLQ Reason: {reason}
System Description: {description}

--- RAW PAYLOAD ---
{payload}

--- INSTRUCTIONS ---
You must output YOUR ENTIRE RESPONSE as a single, valid JSON object. Do not include markdown blocks or conversational text.

1. "suggested_classification": Group the error. Try to map to these existing known classifications first if applicable: ["Schema_Validation_Failed", "Payload_Malformed", "Circuit_Breaker_Open", "Business_Logic_Violation"].
2. "suggested_pattern": A specific, snake_case fingerprint of the error (e.g., missing_mandatory_customer_id).
3. "suggested_action": Choose ONE action from this list: {json.dumps(self.valid_actions)}. If none apply, suggest a new one but prefix it with "custom_" (e.g., "custom_quarantine").
4. "detection_rule": A simple JSONPath string or Regex condition to catch this exact 'suggested_pattern' next time.
5. "confidence_score": A float between 0.0 and 1.0 indicating your certainty.
6. "reasoning_summary": A brief 1-sentence explanation of your choice.

JSON FORMAT:
{{
    "suggested_classification": "...",
    "suggested_pattern": "...",
    "suggested_action": "...",
    "detection_rule": "...",
    "confidence_score": 0.0,
    "reasoning_summary": "..."
}}
"""