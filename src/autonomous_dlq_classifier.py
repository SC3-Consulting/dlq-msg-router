import hashlib
import json
import logging
import os
from pydoc import text
import re
import rule_engine
from azure.servicebus import ServiceBusMessage

class AutonomousDLQClassifier:
    """
    An intelligent, multi-gate triage engine for Dead Letter Queue processing.
    Implements a defense-in-depth architecture to process deterministic faults
    via a Heuristics Engine, while routing unknown anomalies to a constrained AI agent.
    """
    def __init__(self, idempotency_cache, classification_cache, ai_client, database_client, parking_lot_sender, dlq_receiver, source_queue_name):
        self.idempotency_cache = idempotency_cache
        self.classification_cache = classification_cache
        self.ai = ai_client
        self.db = database_client
        self.parking_lot = parking_lot_sender
        self.dlq_receiver = dlq_receiver
        self.source_queue_name = source_queue_name # NEW: Multi-queue observability
        self.logger = logging.getLogger("AutonomousDLQClassifier")
        
        # Load Dynamic Rules Engine
        rules_path = os.getenv("RULES_FILE_PATH", "data/rules.json")
        self.rules = self._load_rules(rules_path)

    def _load_rules(self, filepath):
        """Loads and compiles deterministic routing logic from the external JSON configuration."""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            
            compiled_rules = []
            for r in data.get("routing_rules", []):
                compiled_rules.append({
                    "rule_id": r["rule_id"],
                    "severity_score": r["severity_score"],
                    "classification": r["classification"],
                    "pattern_name": r["pattern_name"],
                    "pattern_regex": r.get("pattern_regex"),
                    "default_action": r.get("default_action", "escalate"), # NEW: Fallback to escalate if missing
                    "engine": rule_engine.Rule(r["condition"]) # Compile string to machine logic
                })

            self.classification_cache.flush()
            self.logger.info("Rules loaded. Classification cache flushed to prevent race conditions.")
            return compiled_rules
        except Exception as e:
            self.logger.error(f"Failed to load rules engine: {e}")
            return []

    def process_batch(self, dlq_messages):
        """Processes a batch of PEEK_LOCKED messages from the broker."""
        for message in dlq_messages:
            try:
                self._classify_single_message(message)
            except Exception as e:
                self.logger.error(f"Critical failure processing message {message.message_id}: {e}")

    def _classify_single_message(self, message):
        # --- 1. SAFE METADATA EXTRACTION ---
        headers = message.application_properties or {}
        message_type = self._safe_decode(headers.get(b'message_type') or headers.get('message_type', 'unknown_type'))
        try:
            self.current_raw_payload_bytes = b"".join(message.body)
            self.current_raw_payload_str = self.current_raw_payload_bytes.decode('utf-8')
        except Exception:
            self.current_raw_payload_bytes = b""
            self.current_raw_payload_str = "unreadable_payload"

        client_id = self._safe_decode(headers.get(b'client_id') or headers.get('client_id', 'unknown_client'))
        correlation_id = message.correlation_id or message.message_id
        
        dlq_reason = message.dead_letter_reason or "Unknown"
        dlq_desc = message.dead_letter_error_description or "Unknown"

        # --- GATE A: ANTI-POISON PILL (TIER 1) ---
        # Isolates message loops that have exhausted infrastructure retry limits.
        resubmit_count = int(headers.get(b'Resubmit-Count') or headers.get('Resubmit-Count', 0))
        if resubmit_count >= 3:
            contract = self._build_contract(
                client_id=client_id, 
                message_type=message_type,
                classification="Resubmit_Limit_Exhausted", 
                pattern="poison_pill_threshold_exceeded", 
                status="Quarantined"
            )
            self._resolve_message(message, contract, route_to_parking_lot=True)
            return

        # --- GATE B: IDEMPOTENCY CHECK ---
        # Cryptographic payload hashing to prevent compute exhaustion from duplicate fault submissions.
        payload_hash = hashlib.sha256(self.current_raw_payload_bytes).hexdigest()
        idemp_string = f"{client_id}_{message_type}_{correlation_id}_{payload_hash}"
        idempotency_hash = hashlib.sha256(idemp_string.encode()).hexdigest()

        # Increment the cache and check how many times we've seen this exact error
        duplicate_count = self.idempotency_cache.increment(idempotency_hash, ttl_seconds=86400)
        
        if duplicate_count > 1:
            # Suppress logging noise for aggressive retry loops exceeding threshold
            status = "Dropped_Threshold_Exceeded_Noise_Suppressed" if duplicate_count > 10 else "Dropped"
            
            contract = self._build_contract(
                client_id=client_id, 
                message_type=message_type,
                classification="Duplicate_Transaction", 
                pattern="exact_correlation_match_in_cache", 
                status=status,
                occurrence_count=duplicate_count
            )
            self._resolve_message(message, contract, route_to_parking_lot=False)
            return
        

        # --- GATE C: CLASSIFICATION CACHE (SPEED OPTIMIZATION) ---
        # Hash the error signature (client + reason + description) for fast deterministic matching.
        error_shape_string = f"{client_id}_{dlq_reason}_{dlq_desc}"
        classification_hash = hashlib.sha256(error_shape_string.encode()).hexdigest()

        if self.classification_cache.exists(classification_hash):
            cached_result = self.classification_cache.get(classification_hash)
            contract = self._build_contract(
                client_id=client_id,
                message_type=message_type,
                classification=cached_result['classification'],
                pattern=cached_result['pattern'],
                status="Auto_Classified_From_Cache",
                suggested_action=cached_result.get('action')
            )
            self._resolve_message(message, contract, route_to_parking_lot=False)
            return

        # --- GATE D: THE HEURISTIC ROUTER (DYNAMIC) ---
        classification, pattern, action = self._evaluate_heuristics(dlq_reason, dlq_desc)

        # --- GATE E: THE AI FALLBACK (TIER 3) ---
        if classification == "Unclassified_Anomaly":
            ai_result = self._invoke_ai_with_salvage(client_id, dlq_reason, dlq_desc, self.current_raw_payload_str)
            
            confidence = float(ai_result.get('confidence_score', 0.0))
            status = "AI_Suggested_Rule_Pending_Approval" if confidence >= 0.80 else "AI_Low_Confidence_Manual_Review"
            
            contract = self._build_contract(
                client_id=client_id,
                message_type=message_type,
                classification=ai_result.get('suggested_classification', 'Unknown_Error'),
                pattern=ai_result.get('suggested_pattern', 'unknown_pattern'),
                status=status,
                ai_reasoning=ai_result.get('reasoning_summary'),
                suggested_action=ai_result.get('suggested_action'),
                detection_rule=ai_result.get('detection_rule'),
                confidence_score=confidence
            )
            # Governance Constraint: AI-generated classifications are isolated to the Parking Lot.
            # They bypass the runtime cache until promoted to rules.json by human operators.
            self._resolve_message(message, contract, route_to_parking_lot=True)
            return

        # --- DETERMINISTIC SUCCESS ---        
        # 10-minute TTL for classifications (allows ops hotfixes to propagate quickly)
        self.classification_cache.save(classification_hash, {
            "classification": classification,
            "pattern": pattern,
            "action": action
        }, ttl_seconds=600)
        
        contract = self._build_contract(
            client_id=client_id, 
            message_type=message_type,
            classification=classification, 
            pattern=pattern, 
            status="Auto_Classified",
            suggested_action=action
        )
        self._resolve_message(message, contract, route_to_parking_lot=False)

    def _evaluate_heuristics(self, reason, description):
        """
        Dynamically evaluates metadata against rules.json using the rule-engine library.
        """
        context = {
            "reason": reason,
            "description": description
        }
        
        matches = []
        for rule in self.rules:
            # Execute the compiled rule_engine logic
            if rule["engine"].matches(context):
                pattern = rule["pattern_name"]
                
                # If a regex exists in the JSON, apply it to the description
                if rule.get("pattern_regex"):
                    regex_match = re.search(rule["pattern_regex"], description, re.IGNORECASE)
                    if regex_match:
                        pattern = f"missing_field_{regex_match.group(1).lower()}"
                
                matches.append((rule["severity_score"], rule["classification"], pattern, rule["default_action"]))

        if not matches:
            return "Unclassified_Anomaly", "unknown", None
            
        # Priority resolution: Lowest severity score determines primary classification
        matches.sort(key=lambda x: x[0])
        best_match = matches[0]
        
        return best_match[1], best_match[2], best_match[3]

    def _invoke_ai_with_salvage(self, client_id, reason, description, payload):
        try:
            raw_text = self.ai.call_llm(client_id, reason, description, payload)
            return self._salvage_json(raw_text)
        except Exception as e:
            self.logger.error(f"LLM Invocation Failed: {str(e)}")
            return {
                "suggested_classification": "AI_UNAVAILABLE", 
                "suggested_action": "Escalate",
                "suggested_pattern": "manual_review_required"
            }

    def _salvage_json(self, raw_text: str) -> dict:
        """Extracts and parses a JSON object from a raw LLM text response."""
        cleaned = raw_text.strip()
        
        # Match a fenced code block like ```json ... ``` or ``` ... ```
        fence_match = re.search(r"(?:```(?:json)?\s*)(.+?)\s*```", cleaned, re.DOTALL)
        if fence_match:
            cleaned = fence_match.group(1).strip()
            
        if not cleaned:
            raise ValueError("Empty LLM response after unwrapping.")
            
        try:
            parsed = json.loads(cleaned)
            if isinstance(parsed, dict): 
                return parsed
        except json.JSONDecodeError:
            pass
            
        start = cleaned.find("{")
        end = cleaned.rfind("}")
        
        if start == -1 or end == -1 or end <= start:
            raise ValueError("No JSON object found")
            
        return json.loads(cleaned[start : end + 1])

    def _build_contract(self, client_id, message_type, classification, pattern, status, ai_reasoning=None, suggested_action=None, detection_rule=None, confidence_score=None, occurrence_count=1):
        error_fingerprint = hashlib.sha256(f"{client_id}_{pattern}".encode()).hexdigest()
        
        contract = {
            "source_queue": self.source_queue_name,
            "source_entity": "Queue", # Hardcoded for MVP, dynamic in Phase 2
            "client_id": client_id,
            "message_type": message_type,
            "classification": classification,
            "pattern": pattern,
            "error_fingerprint": error_fingerprint,
            "status": status,
            "occurrence_count": occurrence_count
        }
        
        if ai_reasoning: contract["ai_reasoning_summary"] = ai_reasoning
        if suggested_action: contract["suggested_action"] = suggested_action
        if detection_rule: contract["detection_rule"] = detection_rule
        if confidence_score is not None: contract["confidence_score"] = confidence_score
            
        return contract

    def _resolve_message(self, message, contract, route_to_parking_lot):
        self.db.log_telemetry(contract)
        
        if route_to_parking_lot:
            safe_properties = {}
            # Sanitize incoming AMQP byte-keys/values to strings for the outgoing message
            if message.application_properties:
                for k, v in message.application_properties.items():
                    safe_properties[self._safe_decode(k)] = self._safe_decode(v)
            
            new_msg = ServiceBusMessage(
                body=self.current_raw_payload_bytes, 
                application_properties=safe_properties, # <--- FIXED: Passing the sanitized dict
                correlation_id=message.correlation_id,
                message_id=message.message_id,
                subject=message.subject,
                content_type=message.content_type
            )
            self.parking_lot.send_messages(new_msg)
            
        self.dlq_receiver.complete_message(message)

    def _safe_decode(self, value):
        if isinstance(value, bytes):
            return value.decode('utf-8')
        return str(value)