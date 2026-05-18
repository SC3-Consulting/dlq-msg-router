import json
import logging
import hashlib
import re
import requests
from datetime import datetime, timedelta, timezone
from azure.identity import DefaultAzureCredential
from azure.servicebus import ServiceBusClient, ServiceBusSubQueue

# Logging setup
logging.basicConfig(level=logging.INFO, format="%(asctime)s - %(levelname)s - %(message)s")
logger = logging.getLogger(__name__)

# CONFIGURATION
FULLY_QUALIFIED_NAMESPACE = "viva-sb-ns-swastik.servicebus.windows.net"
QUEUE_NAME = "viva-integration-queue"
OLLAMA_ENDPOINT = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama-local" # Change this to whichever model you pulled locally (e.g., phi3, mistral)

class VivaDLQTriageAgent:
    def __init__(self):
        logger.info("Initializing Triage Agent & Zero-Trust Credentials...")
        self.credential = DefaultAzureCredential()
        self.rolling_cache = {}
        self.rules_db = self._load_rules()

    def _load_rules(self):
        """Loads the local rules.json file into memory."""
        try:
            with open("data/rules.json", "r") as file:
                return json.load(file).get("rules", {})
        except FileNotFoundError:
            logger.warning("No data/rules.json found. Running strictly on AI Fallback.")
            return {}

    def generate_fingerprint(self, client_id, event_type, reason, description):
        """
        Generates a collision-proof, normalized hash.
        Strips numbers and dynamic data from the description to prevent cache-busting.
        """
        # Normalize description: remove all digits and hex-like patterns
        if description:
            normalized_desc = re.sub(r'\d+', '', description)
            normalized_desc = re.sub(r'0x[a-fA-F0-9]+', '', normalized_desc)
        else:
            normalized_desc = "NO_DESC"

        raw_string = f"{client_id}|{event_type}|{reason}|{normalized_desc}"
        fingerprint = hashlib.md5(raw_string.encode('utf-8')).hexdigest()[:8]
        return fingerprint

    def check_deterministic_rules(self, reason, fingerprint):
        """Checks if we have a hardcoded rule for this error."""
        # Check by explicit ASB reason first, then by MD5 fingerprint
        if reason in self.rules_db:
            return self.rules_db[reason]
        if fingerprint in self.rules_db:
            return self.rules_db[fingerprint]
        return None

    def check_rolling_cache(self, fingerprint):
        """Token protection: Checks if LLM already solved this in the last 10 mins."""
        if fingerprint in self.rolling_cache:
            entry = self.rolling_cache[fingerprint]
            if datetime.now(timezone.utc) - entry["timestamp"] < timedelta(minutes=10):
                return entry["classification"]
        return None

    def call_ai_classifier(self, payload_str, reason, description):
        """Invokes the local Ollama LLM with a strict JSON contract."""
        prompt = f"""
        You are an enterprise integration architect. Classify this Dead Letter Queue message.
        Reason: {reason}
        Description: {description}
        Payload: {payload_str}
        
        Output ONLY a raw JSON object matching this schema, nothing else:
        {{
            "classification": "string",
            "suggested_action": "string",
            "confidence": float
        }}
        """
        
        payload = {
            "model": OLLAMA_MODEL,
            "prompt": prompt,
            "stream": False,
            "format": "json"
        }

        logger.info("Cache Miss: Invoking local LLM...")
        try:
            response = requests.post(OLLAMA_ENDPOINT, json=payload)
            response.raise_for_status()
            return json.loads(response.json().get("response", "{}"))
        except Exception as e:
            logger.error(f"LLM Invocation Failed: {str(e)}")
            return {"error": "AI_UNAVAILABLE"}

    def triage_message(self, message):
        """The core orchestration pipeline."""
        try:
            body_str = str(message)
            data = json.loads(body_str)
            client_id = data.get("payload", {}).get("clientId", "UNKNOWN")
            event_type = data.get("metadata", {}).get("eventType", "UNKNOWN")
            
            # ASB stores DLQ reasons in the application_properties or dead_letter metadata
            reason = message.dead_letter_reason or "UNKNOWN_REASON"
            description = message.dead_letter_error_description or "NO_DESCRIPTION"

            logger.info(f"--- Triaging DLQ Message for Client: {client_id} ---")

            # 1. Fingerprint
            fingerprint = self.generate_fingerprint(client_id, event_type, reason, description)
            logger.info(f"Pattern Fingerprint generated: {fingerprint}")

            # 2. Heuristics Check (Cost: $0, Time: 0ms)
            rule = self.check_deterministic_rules(reason, fingerprint)
            if rule:
                logger.info(f"[HEURISTIC MATCH] Executing action: {rule['action']}")
                return

            # 3. Cache Check (Cost: $0, Time: 0ms)
            cached_result = self.check_rolling_cache(fingerprint)
            if cached_result:
                logger.info(f"[CACHE HIT] Applying previous AI classification for {fingerprint}.")
                logger.info(f"Cached Result: {json.dumps(cached_result)}")
                return

            # 4. AI Fallback (Cost: Compute, Time: 1-5s)
            logger.warning(f"[UNKNOWN PATTERN] Fingerprint {fingerprint} not recognized.")
            ai_result = self.call_ai_classifier(body_str, reason, description)
            
            # 5. Update Cache & Present to Human
            self.rolling_cache[fingerprint] = {
                "timestamp": datetime.now(timezone.utc),
                "classification": ai_result
            }
            logger.info(f"[AI SUGGESTION READY FOR REVIEW] -> {json.dumps(ai_result)}")

        except Exception as e:
            logger.error(f"Failed to triage message: {str(e)}")

    def start_triage_loop(self):
        """Listens explicitly to the Dead Letter sub-queue."""
        logger.info(f"Listening on DLQ for {QUEUE_NAME}...")
        try:
            with ServiceBusClient(FULLY_QUALIFIED_NAMESPACE, self.credential) as client:
                # SubQueue definition is critical to read the DLQ, not the main queue
                with client.get_queue_receiver(queue_name=QUEUE_NAME, sub_queue=ServiceBusSubQueue.DEAD_LETTER, max_wait_time=60) as receiver:
                    for message in receiver:
                        self.triage_message(message)
                        # Complete message removes it from the DLQ after triage
                        receiver.complete_message(message)
        except Exception as e:
            logger.error(f"Triage Loop Error: {str(e)}")

if __name__ == "__main__":
    agent = VivaDLQTriageAgent()
    agent.start_triage_loop()