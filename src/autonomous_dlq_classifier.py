import hashlib
import json
import logging
import os
import re
import rule_engine
from azure.servicebus import ServiceBusMessage

from src.action_executor import ActionRouter

class AutonomousDLQClassifier:
    """
    An intelligent, multi-gate triage engine for Dead Letter Queue processing.
    Implements a defence-in-depth architecture to process deterministic faults
    via a Heuristics Engine, whilst routing unknown anomalies to a constrained AI agent.
    """
    def __init__(self, idempotency_cache, classification_cache, ai_client, database_client, parking_lot_sender, main_queue_sender, dlq_receiver, source_queue_name):
        self.idempotency_cache = idempotency_cache
        self.classification_cache = classification_cache
        self.ai = ai_client
        self.db = database_client
        self.parking_lot = parking_lot_sender
        self.dlq_receiver = dlq_receiver
        self.source_queue_name = source_queue_name 
        self.logger = logging.getLogger("AutonomousDLQClassifier")
        
        self.action_router = ActionRouter(
            receiver=dlq_receiver,
            sender=main_queue_sender,
            parking_lot_sender=parking_lot_sender
        )
        
        # Parameterise cache TTLs and Thresholds from environment variables
        self.idemp_ttl = int(os.getenv("IDEMPOTENCY_TTL_SECONDS", 86400))
        self.class_ttl = int(os.getenv("CLASSIFICATION_TTL_SECONDS", 600))
        self.max_resubmit_count = int(os.getenv("MAX_RESUBMIT_COUNT", 3))
        self.duplicate_noise_threshold = int(os.getenv("DUPLICATE_NOISE_THRESHOLD", 10))
        
        # Load Dynamic Rules Engine
        rules_path = os.getenv("RULES_FILE_PATH", "data/rules.json")
        self.rules = self._load_rules(rules_path)

    def _load_rules(self, filepath):
        """Loads and compiles deterministic routing logic from the external JSON configuration."""
        try:
            with open(filepath, 'r') as f:
                data = json.load(f)
            
            # Merge global rules with queue-specific overrides
            raw_rules = data.get("global_rules", [])
            queue_overrides = data.get("queue_overrides", {}).get(self.source_queue_name, [])
            raw_rules.extend(queue_overrides)

            compiled_rules = []
            for r in raw_rules:
                # CRITICAL PATCH: Isolate rule compilation so one bad syntax doesn't wipe out all rules
                try:
                    compiled_rules.append({
                        "rule_id": r["rule_id"],
                        "severity_score": r["severity_score"],
                        "classification": r["classification"],
                        "pattern_name": r["pattern_name"],
                        "pattern_regex": r.get("pattern_regex"),
                        "default_action": r.get("default_action", "escalate"),
                        "safe_defaults_map": r.get("safe_defaults_map"),
                        "engine": rule_engine.Rule(r["condition"]) 
                    })
                except Exception as rule_err:
                    self.logger.warning(f"Failed to compile rule '{r.get('rule_id', 'unknown')}': {rule_err}")
                    continue

            self.classification_cache.flush()
            self.logger.info(f"Loaded {len(compiled_rules)} deterministic rules for {self.source_queue_name}. Cache flushed.")
            return compiled_rules
        except Exception as e:
            self.logger.error(f"Failed to load rules engine configuration: {e}")
            return []
        
    def process_batch(self, dlq_messages):
        """Processes a batch of PEEK_LOCKED messages from the broker."""
        for message in dlq_messages:
            try:
                self._classify_single_message(message)
            except Exception as e:
                self.logger.error(f"Critical pipeline failure processing message {message.message_id}: {e}", exc_info=True)
                if os.getenv("ENABLE_NESTED_BROKER_EXCEPTIONS", "True").lower() == "true":
                    try:
                        self.dlq_receiver.abandon_message(message)
                    except Exception as abandon_err:
                        self.logger.error(f"Failed to safely abandon message {message.message_id}: {abandon_err}")
                else:
                    self.dlq_receiver.abandon_message(message)

    def _classify_single_message(self, message):
        headers = message.application_properties or {}
        message_type = self._safe_decode(headers.get(b'message_type') or headers.get('message_type', 'unknown_type'))
        
        # Separate byte extraction from string decoding to prevent Idempotency collisions
        try:
            self.current_raw_payload_bytes = b"".join(message.body)
        except Exception:
            self.current_raw_payload_bytes = b""
            
        try:
            self.current_raw_payload_str = self.current_raw_payload_bytes.decode('utf-8', errors='replace')
        except Exception:
            self.current_raw_payload_str = "unreadable_payload"

        client_id = self._safe_decode(headers.get(b'client_id') or headers.get('client_id', 'unknown_client'))
        correlation_id = message.correlation_id or message.message_id
        
        dlq_reason = message.dead_letter_reason or "Unknown"
        dlq_desc = message.dead_letter_error_description or "Unknown"

        # --- GATE A: ANTI-POISON PILL ---
        resubmit_count = int(headers.get(b'Resubmit-Count') or headers.get('Resubmit-Count', 0))
        if resubmit_count >= self.max_resubmit_count:
            contract = self._build_contract(
                client_id=client_id, message_type=message_type,
                classification="Resubmit_Limit_Exhausted", pattern="poison_pill_threshold_exceeded", status="Quarantined"
            )
            self.db.log_telemetry(contract)
            self.action_router.route_and_execute("escalate", message, "poison_pill_threshold_exceeded")
            return

        # --- GATE B: IDEMPOTENCY CHECK ---
        payload_hash = hashlib.sha256(self.current_raw_payload_bytes).hexdigest()
        idemp_string = f"{client_id}_{message_type}_{correlation_id}_{payload_hash}"
        idempotency_hash = hashlib.sha256(idemp_string.encode()).hexdigest()

        duplicate_count = self.idempotency_cache.increment(idempotency_hash, ttl_seconds=self.idemp_ttl)
        
        if duplicate_count > 1:
            status = "Dropped_Threshold_Exceeded_Noise_Suppressed" if duplicate_count > self.duplicate_noise_threshold else "Dropped"
            contract = self._build_contract(
                client_id=client_id, message_type=message_type,
                classification="Duplicate_Transaction", pattern="exact_correlation_match_in_cache", 
                status=status, occurrence_count=duplicate_count
            )
            self.db.log_telemetry(contract)
            self.action_router.route_and_execute("drop", message, "exact_correlation_match_in_cache")
            return
        
        # --- GATE C: CLASSIFICATION CACHE ---
        error_shape_string = f"{client_id}_{dlq_reason}_{dlq_desc}"
        classification_hash = hashlib.sha256(error_shape_string.encode()).hexdigest()

        if self.classification_cache.exists(classification_hash):
            cached_result = self.classification_cache.get(classification_hash)
            contract = self._build_contract(
                client_id=client_id, message_type=message_type,
                classification=cached_result['classification'], pattern=cached_result['pattern'],
                status="Auto_Classified_From_Cache", suggested_action=cached_result.get('action')
            )
            self.db.log_telemetry(contract)
            self.action_router.route_and_execute(cached_result.get('action', 'escalate'), message, cached_result.get('pattern', ''),cached_result.get('safe_defaults_map'))
            return

        # --- GATE D: THE HEURISTIC ROUTER ---
        classification, pattern, action, safe_defaults_map = self._evaluate_heuristics(client_id,dlq_reason, dlq_desc)

        # --- GATE E: AI FALLBACK (AGENTIC DISCOVERY) ---
        if classification == "Unclassified_Anomaly":
            ai_result = self._invoke_ai_with_salvage(client_id, dlq_reason, dlq_desc, self.current_raw_payload_str)
            confidence = self._safe_float(ai_result.get('confidence_score', 0.0))
            status = "AI_Suggested_Rule_Pending_Approval" if confidence >= 0.80 else "AI_Low_Confidence_Manual_Review"
            
            contract = self._build_contract(
                client_id=client_id, message_type=message_type,
                classification=ai_result.get('suggested_classification', 'Unknown_Error'),
                pattern=ai_result.get('suggested_pattern', 'unknown_pattern'),
                status=status, ai_reasoning=ai_result.get('reasoning_summary'),
                suggested_action=ai_result.get('suggested_action'),
                detection_rule=ai_result.get('detection_rule'),
                confidence_score=confidence
            )
            
            self.db.log_telemetry(contract)
            # Governance Constraint: AI anomalies are isolated to Parking Lot via "escalate".
            self.action_router.route_and_execute("escalate", message, ai_result.get('suggested_pattern', 'unknown_pattern'))
            return

        # --- DETERMINISTIC SUCCESS ---        
        self.classification_cache.save(classification_hash, {
            "classification": classification, "pattern": pattern, "action": action, "safe_defaults_map": safe_defaults_map
        }, ttl_seconds=self.class_ttl)
        
        contract = self._build_contract(
            client_id=client_id, message_type=message_type,
            classification=classification, pattern=pattern, status="Auto_Classified", suggested_action=action
        )
        
        self.db.log_telemetry(contract)
        self.action_router.route_and_execute(action, message, pattern, safe_defaults_map)

    def _evaluate_heuristics(self, client_id, reason, description):
        context = {"client_id": client_id,"reason": reason, "description": description}
        matches = []
        for rule in self.rules:
            # Fail-open on malformed rule logic to prevent pipeline crash
            try:
                if rule["engine"].matches(context):
                    pattern = rule["pattern_name"]
                    if rule.get("pattern_regex"):
                        regex_match = re.search(rule["pattern_regex"], description, re.IGNORECASE)
                        if regex_match:
                            pattern = f"missing_field_{regex_match.group(1).lower()}"
                    matches.append((rule["severity_score"], rule["classification"], pattern, rule["default_action"], rule.get("safe_defaults_map")))
            except Exception as e:
                self.logger.warning(f"Rule Engine skipped malformed rule '{rule.get('rule_id', 'unknown')}': {e}")
                continue

        if not matches:
            return "Unclassified_Anomaly", "unknown", None, None
            
        matches.sort(key=lambda x: x[0])
        best_match = matches[0]
        return best_match[1], best_match[2], best_match[3], best_match[4]

    def _invoke_ai_with_salvage(self, client_id, reason, description, payload):
        try:
            # The AI Factory now handles the text salvaging and returns a safe dictionary
            ai_dict = self.ai.call_llm(client_id, reason, description, payload)
            return ai_dict
        except Exception as e:
            self.logger.error(f"LLM Invocation Failed: {str(e)}")
            return {"suggested_classification": "AI_UNAVAILABLE", "suggested_action": "escalate", "suggested_pattern": "manual_review_required"}

    def _build_contract(self, client_id, message_type, classification, pattern, status, ai_reasoning=None, suggested_action=None, detection_rule=None, confidence_score=None, occurrence_count=1):
        error_fingerprint = hashlib.sha256(f"{client_id}_{pattern}".encode()).hexdigest()
        contract = {
            "source_queue": self.source_queue_name, "source_entity": "Queue", 
            "client_id": client_id, "message_type": message_type,
            "classification": classification, "pattern": pattern,
            "error_fingerprint": error_fingerprint, "status": status,
            "occurrence_count": occurrence_count
        }
        if ai_reasoning: contract["ai_reasoning_summary"] = ai_reasoning
        if suggested_action: contract["suggested_action"] = suggested_action
        if detection_rule: contract["detection_rule"] = detection_rule
        if confidence_score is not None: contract["confidence_score"] = confidence_score
            
        return contract

    def _safe_float(self, val):
        """Safely casts LLM confidence scores to float to prevent ValueError crashes."""
        try:
            return float(val)
        except (ValueError, TypeError):
            self.logger.warning(f"Failed to cast AI confidence score '{val}' to float. Defaulting to 0.0.")
            return 0.0

    def _safe_decode(self, value):
        """CRITICAL PATCH: Prevents thread crashes on malformed binary headers."""
        if isinstance(value, bytes):
            return value.decode('utf-8', errors='replace')
        return str(value)