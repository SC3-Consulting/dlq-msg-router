"""
This module implements the AutonomousDLQClassifier, an intelligent, multi-gate triage engine for Dead Letter Queue (DLQ) processing in Azure Service Bus (ASB).
- The classifier employs a defence-in-depth architecture, processing deterministic faults via a Heuristics Engine
  while routing unknown anomalies to a constrained AI agent for classification and remediation.
- The classifier is designed to be resilient, with idempotency checks, classification caching, and
  circuit breaker patterns to handle transient failures and prevent cascading errors.
- The classifier supports dynamic rule loading from an external JSON configuration, allowing for
  flexible and adaptive routing logic without requiring code changes or redeployment.
- The classifier logs detailed telemetry for each processed message, including classification results,
  suggested actions, and correlation context, enabling observability and traceability in complex distributed systems.

N.B. The heuristics engine is the primary deterministic routing mechanism, while the AI engine serves as a fallback for unclassified anomalies.
The heuristics are good for known, repeatable patterns, while the AI engine is intended to provide guidance for novel or ambiguous cases.
The heuristics will always lag novel payload structures or evolving business rules and messages.
"""

import hashlib
import json
import logging
import os
import re
from typing import Dict, Optional

import rule_engine
from azure.servicebus import ServiceBusMessage

from src.action_executor import ActionRouter
from src.resilience import CircuitBreaker, backoff_sleep


class AutonomousDLQClassifier:
    """
    An intelligent, multi-gate triage engine for Dead Letter Queue processing.
    Implements a defence-in-depth architecture to process deterministic faults
    via a Heuristics Engine, whilst routing unknown anomalies to a constrained AI agent.
    Designed to be resilient, with idempotency checks, classification caching, and circuit breaker patterns to handle transient failures and prevent cascading errors.
    Supports dynamic rule loading from an external JSON configuration, allowing for flexible and adaptive routing logic without requiring code changes or redeployment.
    Logs detailed telemetry for each processed message, including classification results, suggested actions, and correlation context, enabling observability and traceability in complex distributed systems.
    Attributes:
        idempotency_cache (IdempotencyStore): A disk-backed, thread-safe key-value store for tracking message hashes and preventing duplicate processing.
        classification_cache (ClassificationCache): A memory-backed, thread-safe LRU cache for high-speed deterministic pattern matching and classification of messages.
        ai (BaseAIEngine): An AI engine instance for classifying unknown anomalies using either Ollama or Azure Foundry.
        db (DatabaseClient): A database client for logging telemetry and storing classification results.
        parking_lot (ServiceBusSender): A Service Bus sender for routing messages to a parking lot queue for manual review.
        dlq_receiver (ServiceBusReceiver): A Service Bus receiver for receiving messages from the Dead Letter Queue.
        source_queue_name (str): The name of the source queue being monitored and processed by the classifier.
        main_queue_sender (ServiceBusSender): A Service Bus sender for routing messages back to the main queue after processing.
    Methods:
        process_batch(dlq_messages): Processes a batch of PEEK_LOCKED messages from the broker, applying classification and routing logic.
        _classify_single_message(message): Classifies a single DLQ message, applying idempotency checks, classification caching, heuristic evaluation, and AI fallback as needed.
        _evaluate_heuristics(client_id, reason, description): Evaluates deterministic rules against the message context to determine classification and routing actions.
        _invoke_ai_with_salvage(client_id, reason, description, payload): Invokes the AI engine to classify unknown anomalies and returns the suggested classification and action.
        _build_contract(...): Constructs a telemetry contract for logging classification results and suggested actions.
        _extract_correlation_context(message, headers): Extracts correlation context from message headers for observability and traceability.
        _load_rules(filepath): Loads and compiles deterministic routing logic from an external JSON configuration file.
    """

    def __init__(
        self,
        idempotency_cache,
        classification_cache,
        ai_client,
        database_client,
        parking_lot_sender,
        main_queue_sender,
        dlq_receiver,
        source_queue_name,
    ):
        """
        Initialises the AutonomousDLQClassifier with the provided components and configuration.
        Args:
            idempotency_cache (IdempotencyStore): A disk-backed, thread-safe key-value store for tracking message hashes and preventing duplicate processing.
            classification_cache (ClassificationCache): A memory-backed, thread-safe LRU cache for high-speed deterministic pattern matching and classification of messages.
            ai_client (BaseAIEngine): An AI engine instance for classifying unknown anomalies using either Ollama or Azure Foundry.
            database_client (DatabaseClient): A database client for logging telemetry and storing classification results.
            parking_lot_sender (ServiceBusSender): A Service Bus sender for routing messages to a parking lot queue for manual review.
            main_queue_sender (ServiceBusSender): A Service Bus sender for routing messages back to the main queue after processing.
            dlq_receiver (ServiceBusReceiver): A Service Bus receiver for receiving messages from the Dead Letter Queue.
            source_queue_name (str): The name of the source queue being monitored and processed by the classifier.
        """
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
            parking_lot_sender=parking_lot_sender,
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
        """Loads and compiles deterministic routing logic from the external JSON configuration.
        Args:
            filepath (str): The path to the JSON file containing the rules configuration.
        Returns:
            list: A list of compiled rule dictionaries ready for evaluation.
        """
        try:
            with open(filepath, "r") as f:
                data = json.load(f)

            # Merge global rules with queue-specific overrides
            raw_rules = data.get("global_rules", [])
            queue_overrides = data.get("queue_overrides", {}).get(
                self.source_queue_name, []
            )
            raw_rules.extend(queue_overrides)

            # TODO: Consider validating the rules schema against a JSON Schema or Pydantic model to ensure that all required fields are present and correctly typed before attempting to compile them.
            # This can prevent runtime errors and improve maintainability.

            # TODO: The safe_defaults_map is not currently used in the rule evaluation logic.
            # Consider implementing logic to apply safe defaults when a rule matches, or remove the field if it is not needed. If kept, then consder:
            # At present, it encodes business semantics directly in a JSON file, which is not ideal.
            # A bad default could produce a syntactically valid but semantically wrong message, which could be worse than dropping the message entirely.
            # Consider adding validation or constraints on the safe defaults to ensure they are valid and safe to apply.
            # Consider moving this logic into the codebase or a more structured configuration format.

            compiled_rules = []
            for r in raw_rules:
                # CRITICAL PATCH: Isolate rule compilation so one bad syntax doesn't wipe out all rules
                try:
                    compiled_rules.append(
                        {
                            "rule_id": r["rule_id"],
                            "severity_score": r["severity_score"],
                            "classification": r["classification"],
                            "pattern_name": r["pattern_name"],
                            "pattern_regex": r.get("pattern_regex"),
                            "default_action": r.get("default_action", "escalate"),
                            "safe_defaults_map": r.get("safe_defaults_map"),
                            "engine": rule_engine.Rule(r["condition"]),
                        }
                    )
                except Exception as rule_err:
                    self.logger.warning(
                        f"Failed to compile rule '{r.get('rule_id', 'unknown')}': {rule_err}"
                    )
                    continue

            # TODO: The classification cache flushes the entire cache on rule reload. This assumes one active ruleset and one cache scope per process.
            # If multiple queues or namespaces share the same process, a reload of one queue's rules will flush the cache for all queues.
            # This can be mitigated by scoping the cache to queue names or namespaces, or by implementing a more granular cache invalidation strategy.
            self.classification_cache.flush()
            self.logger.info(
                f"Loaded {len(compiled_rules)} deterministic rules for {self.source_queue_name}. Cache flushed."
            )
            return compiled_rules
        except Exception as e:
            self.logger.error(f"Failed to load rules engine configuration: {e}")
            return []

    def process_batch(self, dlq_messages):
        """Processes a batch of PEEK_LOCKED messages from the broker.
        Args:
            dlq_messages (list): A list of messages retrieved from the Dead Letter Queue.
        """
        for message in dlq_messages:
            try:
                self._classify_single_message(message)
            except Exception as e:
                self.logger.error(
                    f"Critical pipeline failure processing message {message.message_id}: {e}",
                    exc_info=True,
                )
                if (
                    os.getenv("ENABLE_NESTED_BROKER_EXCEPTIONS", "True").lower()
                    == "true"
                ):
                    try:
                        self.dlq_receiver.abandon_message(message)
                    except Exception as abandon_err:
                        self.logger.error(
                            f"Failed to safely abandon message {message.message_id}: {abandon_err}"
                        )
                else:
                    self.dlq_receiver.abandon_message(message)

    def _classify_single_message(self, message):
        """Classifies a single DLQ message, applying idempotency checks, classification caching, heuristic evaluation, and AI fallback as needed.
        Args:
            message (ServiceBusReceivedMessage): The message retrieved from the Dead Letter Queue.
        """
        headers = message.application_properties or {}
        message_type = self._safe_decode(
            headers.get(b"message_type") or headers.get("message_type", "unknown_type")
        )

        # Separate byte extraction from string decoding to prevent Idempotency collisions
        try:
            self.current_raw_payload_bytes = b"".join(message.body)
        except Exception:
            self.current_raw_payload_bytes = b""

        try:
            self.current_raw_payload_str = self.current_raw_payload_bytes.decode(
                "utf-8", errors="replace"
            )
        except Exception:
            self.current_raw_payload_str = "unreadable_payload"

        # TODO: Idempotency is anchored on ClientID + MessageType + CorrelationID + PayloadHash. This is a heuristic best effort approach and may not be universally applicable.
        # If correlation metadata is missing, duplicated payloads can be over-collapsed; if a legitimate retry changes payload or tracing metadata,
        # duplicates can slip through. This is an at-least-once safeguard, not exactly-once protection.
        # Consider allowing clients to provide a unique idempotency key in the message headers for more robust deduplication.
        # This could be dynamically generated by the client or derived from a combination of business identifiers, and would be validated against the idempotency store.
        # Challenge is that not all clients may have the ability to generate or provide such a key, and it may require additional coordination or schema changes on the client side.
        # Building in dynamic idempotency key mapping and validation to be a future enhancement, potentially with a pluggable strategy pattern
        # to allow different clients to provide their own idempotency keys or use a default hashing strategy.
        # Also see state_managers.py for the IdempotencyStore implementation, which is a disk-backed key-value store with TTL and concurrency control.
        client_id = self._safe_decode(
            headers.get(b"client_id") or headers.get("client_id", "unknown_client")
        )
        correlation_context = self._extract_correlation_context(message, headers)
        correlation_anchor = correlation_context.get(
            "trace_id"
        ) or correlation_context.get("correlation_id")

        dlq_reason = message.dead_letter_reason or "Unknown"
        dlq_desc = message.dead_letter_error_description or "Unknown"

        # --- GATE A: ANTI-POISON PILL ---
        resubmit_count = int(
            headers.get(b"Resubmit-Count") or headers.get("Resubmit-Count", 0)
        )
        if resubmit_count >= self.max_resubmit_count:
            contract = self._build_contract(
                client_id=client_id,
                message_type=message_type,
                classification="Resubmit_Limit_Exhausted",
                pattern="poison_pill_threshold_exceeded",
                status="Quarantined",
                correlation_context=correlation_context,
            )
            self.db.log_telemetry(contract)
            self.action_router.route_and_execute(
                "escalate", message, "poison_pill_threshold_exceeded"
            )
            return

        # --- GATE B: IDEMPOTENCY CHECK ---
        payload_hash = hashlib.sha256(self.current_raw_payload_bytes).hexdigest()
        idemp_string = f"{client_id}_{message_type}_{correlation_anchor}_{payload_hash}"
        idempotency_hash = hashlib.sha256(idemp_string.encode()).hexdigest()

        duplicate_count = self.idempotency_cache.increment(
            idempotency_hash, ttl_seconds=self.idemp_ttl
        )

        if duplicate_count > 1:
            status = (
                "Dropped_Threshold_Exceeded_Noise_Suppressed"
                if duplicate_count > self.duplicate_noise_threshold
                else "Dropped"
            )
            contract = self._build_contract(
                client_id=client_id,
                message_type=message_type,
                classification="Duplicate_Transaction",
                pattern="exact_correlation_match_in_cache",
                status=status,
                occurrence_count=duplicate_count,
                correlation_context=correlation_context,
            )
            self.db.log_telemetry(contract)
            self.action_router.route_and_execute(
                "drop", message, "exact_correlation_match_in_cache"
            )
            return

        # --- GATE C: CLASSIFICATION CACHE ---
        error_shape_string = f"{client_id}_{dlq_reason}_{dlq_desc}"
        classification_hash = hashlib.sha256(error_shape_string.encode()).hexdigest()

        if self.classification_cache.exists(classification_hash):
            cached_result = self.classification_cache.get(classification_hash)
            contract = self._build_contract(
                client_id=client_id,
                message_type=message_type,
                classification=cached_result["classification"],
                pattern=cached_result["pattern"],
                status="Auto_Classified_From_Cache",
                suggested_action=cached_result.get("action"),
                correlation_context=correlation_context,
            )
            self.db.log_telemetry(contract)
            self.action_router.route_and_execute(
                cached_result.get("action", "escalate"),
                message,
                cached_result.get("pattern", ""),
                cached_result.get("safe_defaults_map"),
            )
            return

        # --- GATE D: THE HEURISTIC ROUTER ---
        classification, pattern, action, safe_defaults_map = self._evaluate_heuristics(
            client_id, dlq_reason, dlq_desc
        )

        # --- GATE E: AI FALLBACK (AGENTIC DISCOVERY) ---
        if classification == "Unclassified_Anomaly":
            ai_result = self._invoke_ai_with_salvage(
                client_id, dlq_reason, dlq_desc, self.current_raw_payload_str
            )
            confidence = self._safe_float(ai_result.get("confidence_score", 0.0))
            status = (
                "AI_Suggested_Rule_Pending_Approval"
                if confidence >= 0.80
                else "AI_Low_Confidence_Manual_Review"
            )

            # TODO: Add a structured schema validation step to prevent malformed AI responses from being logged or acted upon.
            # This could include checking for required fields, data types, and value ranges before constructing the telemetry contract or executing actions.
            # In particular, check that suggested_classification, suggested_action, detection_rule, and suggested_pattern are present and valid and confidence_score is within a reasonable range before proceeding.
            contract = self._build_contract(
                client_id=client_id,
                message_type=message_type,
                classification=ai_result.get(
                    "suggested_classification", "Unknown_Error"
                ),
                pattern=ai_result.get("suggested_pattern", "unknown_pattern"),
                status=status,
                ai_reasoning=ai_result.get("reasoning_summary"),
                suggested_action=ai_result.get("suggested_action"),
                detection_rule=ai_result.get("detection_rule"),
                confidence_score=confidence,
                correlation_context=correlation_context,
            )

            self.db.log_telemetry(contract)
            # Governance Constraint: AI anomalies are isolated to Parking Lot via "escalate".
            self.action_router.route_and_execute(
                "escalate",
                message,
                ai_result.get("suggested_pattern", "unknown_pattern"),
            )
            return

        # --- DETERMINISTIC SUCCESS ---
        self.classification_cache.save(
            classification_hash,
            {
                "classification": classification,
                "pattern": pattern,
                "action": action,
                "safe_defaults_map": safe_defaults_map,
            },
            ttl_seconds=self.class_ttl,
        )

        contract = self._build_contract(
            client_id=client_id,
            message_type=message_type,
            classification=classification,
            pattern=pattern,
            status="Auto_Classified",
            suggested_action=action,
            correlation_context=correlation_context,
        )

        self.db.log_telemetry(contract)
        resolved_action = action or "escalate"
        self.action_router.route_and_execute(
            resolved_action, message, pattern, safe_defaults_map
        )

    def _evaluate_heuristics(self, client_id, reason, description):
        """Evaluates deterministic rules against the message context to determine classification and routing actions.
        Args:
            client_id (str): The identifier for the client sending the message.
            reason (str): The reason for dead-lettering the message, used to provide context to the AI model.
            description (str): A description of the dead-letter reason, providing additional context for the AI model.
        Returns:
            tuple: A tuple containing the classification, pattern, suggested action, and safe defaults map.
        """

        # TODO: Every DLQ message is evaluated against all rules - O(n) per message, which can be inefficient for large rule sets.
        # Consider implementing a rule indexing or categorisation strategy to reduce the number of evaluations per message,
        # such as grouping rules by client_id or using reason patterns prefilters, or compiling a lookup table for common dead-letter reasons.

        # TODO: Consider adding addtional metadata fields to support more advanced rule management and lifecycle control to the rules.json schema.
        # Suggested fields: schema_version, owner, priority (renaming severity_score if that is its intent), enabled flag, last_updated timestamp, scope, and rule_type
        # Only add new fields if they are actually used in the rule evaluation logic or for governance purposes, to avoid unnecessary complexity.

        # TODO: Rule application is narrow and string-exact. Should track unmatched dead-letter reasons and add rules incrementally once in production.
        # Rules should cover 80%+ of historical dead-letter reasons to reduce load on the AI engine and improve deterministic routing.
        # The rules only cover a few broker reasons such as TTL expiry, delivery limit, validation failed, malformed message, and circuit breaker in rules.json.
        # Anything outside of these reasons will be classified as "Unclassified_Anomaly" and routed to the AI engine, increasing the load on the AI and potentially delaying resolution.
        # Consider expanding the rules to cover a wider range of broker reasons and error conditions, such as:
        # transient broker unavailability (service busy, timeout, network) schema validation failures, transfer hop count exceeded (routing loop), message/header size exceeded, broker lock lost during processing,
        # required field missing and no safe defaults, primitive type mismatch that is coercible (string to number, etc.), unsupported content type for target consumer,
        # duplicate replay, downstream permanent business rejection (non-retryable validation).
        # Resubmit count threshold exceeded (poison pill), same message dead-lettered repeatedly within a short time window, message body or header encoding issues,
        # Operational scenarios: rule execution failure or malformed rule condition, rule match ambiguity or conflict, rule match with no suggested action or safe defaults,
        # rule match with unsafe defaults that could cause downstream failures, cache/store unavailability during dedupe check (fail-open vs fail-safe policy),
        # cache/store corruption or data loss, cache/store TTL expiry before processing completes, cache/store race conditions or concurrency issues,
        # cache/store size limits exceeded, cache/store eviction of active entries, cache/store serialization/deserialization errors, cache/store network partitioning or latency spikes,
        # Busines scenarios: account sync missing customer identifier, payment request missing required fields such as amount or currency, negative or zero payment amount,
        # invalid currency code, unsupported payment method, expired or invalid authentication token, missing or malformed signature, duplicate transaction detected, unknown product code,
        # event schema version unsupported, event timestamp too far in the future or too old, event source unrecognised, idempotency key missing for operations requiring exactly-once semantics,
        # confidential data policy violations.

        # TODO: Expand matching beyond exact string matches to include regex, fuzzy matching, or semantic similarity for more robust classification of evolving message structures and error conditions.

        # TODO: Pattern_regex is only applied to the description field, not the payload or headers. Consider applying it to other fields such as reason or client_id for more flexible pattern matching.

        # TODO: The queue override mechanism is currently limited to a single queue name.
        # Consider supporting multiple queues or namespaces with different rule sets, enable/disable flags, explicit priorities, potentially using a hierarchical or namespaced configuration structure.

        context = {"client_id": client_id, "reason": reason, "description": description}
        matches = []
        for rule in self.rules:
            # Fail-open on malformed rule logic to prevent pipeline crash
            try:
                if rule["engine"].matches(context):
                    pattern = rule["pattern_name"]
                    if rule.get("pattern_regex"):
                        regex_match = re.search(
                            rule["pattern_regex"], description, re.IGNORECASE
                        )
                        if regex_match:
                            pattern = f"missing_field_{regex_match.group(1).lower()}"
                    matches.append(
                        (
                            rule["severity_score"],
                            rule["classification"],
                            pattern,
                            rule["default_action"],
                            rule.get("safe_defaults_map"),
                        )
                    )
            except Exception as e:
                self.logger.warning(
                    f"Rule Engine skipped malformed rule '{rule.get('rule_id', 'unknown')}': {e}"
                )
                continue

        if not matches:
            return "Unclassified_Anomaly", "unknown", None, None

        # TODO: It is not clear with severity_score, whether a lower score is more severe or less severe.
        # This should be clarified in the rules documentation and enforced in the rule loading logic.
        # Conventionally, a higher severity score should indicate a more severe issue, but the current implementation sorts by ascending severity_score,
        # which may lead to counterintuitive behaviour. It is more of a precedence selector than a severity metric.

        # TODO: The severity_score is acting as a precedence mechanism for rule selection, but it is not clear how to handle ties or conflicting rules.
        # This should be clarified in the rules documentation and enforced in the rule loading logic.

        matches.sort(key=lambda x: x[0])
        best_match = matches[0]
        return best_match[1], best_match[2], best_match[3], best_match[4]

    def _invoke_ai_with_salvage(self, client_id, reason, description, payload):
        """Invokes the AI engine with a salvage mechanism, including circuit breaker and retry logic.
        Args:
            client_id (str): The identifier for the client sending the message.
            reason (str): The reason for dead-lettering the message, used to provide context to the AI model.
            description (str): A description of the dead-letter reason, providing additional context for the AI model.
            payload (str): The raw message payload to be classified and remediated by the AI model.
        Returns:
            dict: A dictionary containing the AI model's classification results or a fallback response if the AI is unavailable.
        """
        circuit = CircuitBreaker("ai")
        ai_max_attempts = int(os.getenv("AI_RETRY_MAX_ATTEMPTS", "2"))
        ai_backoff_base = float(os.getenv("AI_BACKOFF_BASE_SECONDS", "2.0"))
        ai_backoff_max = float(os.getenv("AI_BACKOFF_MAX_SECONDS", "30.0"))

        if not circuit.allow_request():
            self.logger.warning(
                "[CircuitBreaker:ai] OPEN — skipping AI call, escalating to parking lot."
            )
            return {
                "suggested_classification": "AI_UNAVAILABLE",
                "suggested_action": "escalate",
                "suggested_pattern": "manual_review_required",
            }

        # TODO: JSON salvage is a best-effort approach to handle malformed or unexpected payloads.
        # Consider implementing a more robust schema validation and transformation pipeline to ensure that the AI model receives well-formed input,
        # potentially using Pydantic or Marshmallow for schema enforcement.
        # For a noisy or adversarial input stream, consider implementing a pre-processing step to clean and normalise the payload before passing it to the AI model.

        for attempt in range(ai_max_attempts):
            try:
                ai_dict = self.ai.call_llm(client_id, reason, description, payload)
                circuit.record_success()
                return ai_dict
            except Exception as e:
                circuit.record_failure()
                if attempt < ai_max_attempts - 1:
                    sleep_dur = backoff_sleep(
                        attempt,
                        base_seconds=ai_backoff_base,
                        max_seconds=ai_backoff_max,
                    )
                    self.logger.warning(
                        f"LLM invocation failed (attempt {attempt + 1}/{ai_max_attempts}, "
                        f"backoff {sleep_dur:.1f}s): {e}"
                    )
                else:
                    self.logger.error(
                        f"LLM Invocation Failed after {ai_max_attempts} attempts: {str(e)}"
                    )

        return {
            "suggested_classification": "AI_UNAVAILABLE",
            "suggested_action": "escalate",
            "suggested_pattern": "manual_review_required",
        }

    def _build_contract(
        self,
        client_id,
        message_type,
        classification,
        pattern,
        status,
        ai_reasoning=None,
        suggested_action=None,
        detection_rule=None,
        confidence_score=None,
        occurrence_count=1,
        correlation_context=None,
    ):
        """Constructs a telemetry contract for logging classification results and suggested actions.
        Args:
            client_id (str): The identifier for the client sending the message.
            message_type (str): The type of the message being processed.
            classification (str): The classification assigned to the message.
            pattern (str): The pattern name or identifier associated with the classification.
            status (str): The processing status of the message (e.g., Auto_Classified, Escalated).
            ai_reasoning (Optional[str]): A summary of the AI model's reasoning for its classification decision.
            suggested_action (Optional[str]): The action suggested by the classifier for routing or handling the message.
            detection_rule (Optional[str]): The rule identifier that triggered the classification, if applicable.
            confidence_score (Optional[float]): The confidence score from the AI model, if applicable.
            occurrence_count (int): The number of times this message has been processed or encountered.
            correlation_context (Optional[dict]): A dictionary containing correlation identifiers for observability and traceability.
        """
        error_fingerprint = hashlib.sha256(
            f"{client_id}_{pattern}".encode()
        ).hexdigest()
        contract = {
            "source_queue": self.source_queue_name,
            "source_entity": "Queue",
            "client_id": client_id,
            "message_type": message_type,
            "classification": classification,
            "pattern": pattern,
            "error_fingerprint": error_fingerprint,
            "status": status,
            "occurrence_count": occurrence_count,
        }

        if correlation_context:
            contract.update(
                {
                    "correlation_id": correlation_context.get("correlation_id"),
                    "trace_id": correlation_context.get("trace_id"),
                    "span_id": correlation_context.get("span_id"),
                    "traceparent": correlation_context.get("traceparent"),
                    "tracestate": correlation_context.get("tracestate"),
                    "diagnostic_id": correlation_context.get("diagnostic_id"),
                    "correlation_source": correlation_context.get("correlation_source"),
                }
            )

        if ai_reasoning:
            contract["ai_reasoning_summary"] = ai_reasoning
        if suggested_action:
            contract["suggested_action"] = suggested_action
        if detection_rule:
            contract["detection_rule"] = detection_rule
        if confidence_score is not None:
            contract["confidence_score"] = confidence_score

        return contract

    def _extract_correlation_context(
        self, message, headers
    ) -> Dict[str, Optional[str]]:
        """Extracts correlation context from message headers for observability and traceability.
        Args:
            message: The message object containing metadata and identifiers.
            headers: A dictionary of message headers potentially containing tracing information.
        Returns:
            dict: A dictionary containing correlation identifiers and tracing information.
        """
        traceparent = self._first_header(
            headers, ["traceparent", "Traceparent", "Diagnostic-Id", "diagnostic-id"]
        )
        tracestate = self._first_header(headers, ["tracestate", "Tracestate"])
        diagnostic_id = self._first_header(headers, ["Diagnostic-Id", "diagnostic-id"])

        trace_id = self._first_header(
            headers, ["trace_id", "traceid", "x-b3-traceid", "otel.trace_id"]
        )
        span_id = self._first_header(
            headers, ["span_id", "spanid", "x-b3-spanid", "otel.span_id"]
        )

        if traceparent:
            match = re.match(
                r"^00-([0-9a-fA-F]{32})-([0-9a-fA-F]{16})-[0-9a-fA-F]{2}$", traceparent
            )
            if match:
                trace_id = match.group(1).lower()
                span_id = match.group(2).lower()

        correlation_id = message.correlation_id or message.message_id
        correlation_source = "message_id"
        if traceparent and trace_id:
            correlation_source = "otel_traceparent"
        elif trace_id:
            correlation_source = "otel_trace_id"
        elif message.correlation_id:
            correlation_source = "broker_correlation_id"

        return {
            "correlation_id": str(correlation_id) if correlation_id else None,
            "trace_id": trace_id,
            "span_id": span_id,
            "traceparent": traceparent,
            "tracestate": tracestate,
            "diagnostic_id": diagnostic_id,
            "correlation_source": correlation_source,
        }

    def _first_header(self, headers, keys):
        """Extracts the first available header value from a list of potential keys, handling both string and byte representations.
        Args:
            headers (dict): A dictionary of message headers.
            keys (list): A list of potential header keys to search for.
        Returns:
            str: The first matching header value, or None if no match is found.
        """
        for key in keys:
            byte_key = key.encode("utf-8")
            if key in headers:
                return self._safe_decode(headers[key])
            if byte_key in headers:
                return self._safe_decode(headers[byte_key])
        return None

    def _safe_float(self, val):
        """Safely casts LLM confidence scores to float to prevent ValueError crashes.
        Args:
            val: The value to be cast to float.
        Returns:
            float: The casted float value, or 0.0 if casting fails.
        """
        try:
            return float(val)
        except (ValueError, TypeError):
            self.logger.warning(
                f"Failed to cast AI confidence score '{val}' to float. Defaulting to 0.0."
            )
            return 0.0

    def _safe_decode(self, value):
        """Prevents thread crashes on malformed binary headers.
        Args:
            value: The value to be decoded.
        Returns:
            str: The decoded string value, or a replacement string if decoding fails.
        """
        if isinstance(value, bytes):
            return value.decode("utf-8", errors="replace")
        return str(value)
