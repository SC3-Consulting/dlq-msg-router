#!/usr/bin/env python3
"""Generate lightweight performance baselines for DLQ classification flow.

This script measures:
- Throughput (messages/second)
- Per-message latency percentiles (p50/p95/p99)
- Estimated AI cost per batch (token-based)

It is intentionally local-only and dependency-light so it can run in CI or on a developer
machine without external broker or model calls.
"""

import argparse
import json
import math
import os
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List
from unittest.mock import MagicMock

from src.autonomous_dlq_classifier import AutonomousDLQClassifier
from src.state_managers import ClassificationCache, IdempotencyStore


@dataclass
class MockMessage:
    body_dict: Dict
    properties: Dict
    reason: str
    desc: str
    message_id: str

    """
    A mock message class to simulate the behaviour of Azure Service Bus messages for performance testing.
    This class is used to create test messages with specific properties, reasons, and descriptions
    without requiring an actual Service Bus connection. It provides the necessary attributes and methods
    to mimic the interface of real messages, allowing the AutonomousDLQClassifier to process them in
    a controlled test environment.
    Attributes:
        body_dict (Dict): The message body as a dictionary, which will be serialised to JSON.
        properties (Dict): A dictionary of message properties, simulating application properties.
        reason (str): The reason for dead-lettering the message, used to simulate different failure scenarios.
        desc (str): A description of the dead-letter reason, providing additional context for testing.
        message_id (str): A unique identifier for the message, used to track and differentiate messages in tests.
    Methods:
        __post_init__(): Initialises the message attributes after the dataclass is created, setting up the body, application properties, dead-letter reason, and other necessary fields for processing.
    """

    def __post_init__(self):
        self.body = [json.dumps(self.body_dict).encode("utf-8")]
        self.application_properties = self.properties
        self.dead_letter_reason = self.reason
        self.dead_letter_error_description = self.desc
        self.content_type = "application/json"
        self.subject = "PerfSubject"
        self.correlation_id = f"corr-{self.message_id}"


def _percentile(values: List[float], p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    rank = (len(values) - 1) * p
    low = math.floor(rank)
    high = math.ceil(rank)
    if low == high:
        return values[low]
    frac = rank - low
    return values[low] * (1.0 - frac) + values[high] * frac


def _build_classifier(tmp_db_path: str) -> AutonomousDLQClassifier:
    receiver = MagicMock()
    sender = MagicMock()
    parking = MagicMock()
    db = MagicMock()

    ai = MagicMock()
    ai.call_llm.return_value = {
        "suggested_classification": "AI_Classified_Fault",
        "suggested_pattern": "ai_unknown_error",
        "suggested_action": "escalate",
        "confidence_score": 0.91,
    }

    # Keep local benchmark deterministic and fast.
    os.environ["ACTION_RETRY_MAX_ATTEMPTS"] = "1"
    os.environ["DRAIN_RETRY_MAX_ATTEMPTS"] = "1"
    os.environ["AI_RETRY_MAX_ATTEMPTS"] = "1"
    os.environ["AI_BACKOFF_BASE_SECONDS"] = "0"
    os.environ["AI_BACKOFF_MAX_SECONDS"] = "0"
    os.environ["CLASSIFICATION_TTL_SECONDS"] = "600"

    return AutonomousDLQClassifier(
        idempotency_cache=IdempotencyStore(db_path=tmp_db_path),
        classification_cache=ClassificationCache(),
        ai_client=ai,
        database_client=db,
        parking_lot_sender=parking,
        main_queue_sender=sender,
        dlq_receiver=receiver,
        source_queue_name="integration-queue",
    )


def _run_scenario(
    classifier: AutonomousDLQClassifier, message_count: int, ai_ratio: float
) -> Dict:
    latencies_ms: List[float] = []
    ai_count = 0

    start = time.perf_counter()
    for i in range(message_count):
        force_ai = (i / max(1, message_count)) < ai_ratio
        if force_ai:
            reason = "SystemFault"
            desc = "Unexpected null pointer in pipeline"
            ai_count += 1
        else:
            reason = "ValidationFailed"
            desc = "missing mandatory field: 'transaction_amount'"

        msg = MockMessage(
            body_dict={"id": i, "v": f"m-{i}"},
            properties={b"client_id": "Perf_Client", b"Resubmit-Count": 0},
            reason=reason,
            desc=desc,
            message_id=f"perf-{i}",
        )

        t0 = time.perf_counter()
        classifier.process_batch([msg])
        t1 = time.perf_counter()
        latencies_ms.append((t1 - t0) * 1000.0)

    end = time.perf_counter()

    total_seconds = max(1e-9, end - start)
    sorted_lat = sorted(latencies_ms)

    return {
        "message_count": message_count,
        "ai_message_count": ai_count,
        "total_seconds": round(total_seconds, 6),
        "throughput_msg_per_sec": round(message_count / total_seconds, 2),
        "latency_ms": {
            "p50": round(_percentile(sorted_lat, 0.50), 3),
            "p95": round(_percentile(sorted_lat, 0.95), 3),
            "p99": round(_percentile(sorted_lat, 0.99), 3),
            "max": round(sorted_lat[-1] if sorted_lat else 0.0, 3),
        },
    }


def _estimate_ai_cost(ai_message_count: int) -> Dict:
    # Defaults are placeholders; override for your provider/model contract.
    input_tokens_per_msg = int(os.getenv("AI_EST_INPUT_TOKENS_PER_MESSAGE", "900"))
    output_tokens_per_msg = int(os.getenv("AI_EST_OUTPUT_TOKENS_PER_MESSAGE", "120"))
    price_input_per_1k = float(os.getenv("AI_PRICE_INPUT_PER_1K_USD", "0.005"))
    price_output_per_1k = float(os.getenv("AI_PRICE_OUTPUT_PER_1K_USD", "0.015"))

    total_input_tokens = ai_message_count * input_tokens_per_msg
    total_output_tokens = ai_message_count * output_tokens_per_msg

    input_cost = (total_input_tokens / 1000.0) * price_input_per_1k
    output_cost = (total_output_tokens / 1000.0) * price_output_per_1k

    return {
        "assumptions": {
            "input_tokens_per_message": input_tokens_per_msg,
            "output_tokens_per_message": output_tokens_per_msg,
            "price_input_per_1k_usd": price_input_per_1k,
            "price_output_per_1k_usd": price_output_per_1k,
        },
        "estimated": {
            "total_input_tokens": total_input_tokens,
            "total_output_tokens": total_output_tokens,
            "input_cost_usd": round(input_cost, 6),
            "output_cost_usd": round(output_cost, 6),
            "total_cost_usd": round(input_cost + output_cost, 6),
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run local DLQ performance baseline benchmark"
    )
    parser.add_argument(
        "--sizes", default="100,500,1000", help="Comma-separated message batch sizes"
    )
    parser.add_argument(
        "--ai-ratio",
        type=float,
        default=0.20,
        help="Fraction of messages routed to AI fallback",
    )
    parser.add_argument(
        "--output",
        default="reports/performance_baseline.json",
        help="Path to output JSON report",
    )
    args = parser.parse_args()

    sizes = [int(s.strip()) for s in args.sizes.split(",") if s.strip()]
    output_path = Path(args.output)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as tmpdir:
        db_path = str(Path(tmpdir) / "perf_idempotency.db")
        classifier = _build_classifier(db_path)

        scenarios = []
        for size in sizes:
            scenario = _run_scenario(classifier, size, args.ai_ratio)
            scenario["ai_cost"] = _estimate_ai_cost(scenario["ai_message_count"])
            scenarios.append(scenario)

    report = {
        "generated_at_epoch": int(time.time()),
        "environment": {
            "python": os.getenv("PYTHON_VERSION", "unknown"),
            "ai_ratio": args.ai_ratio,
        },
        "scenarios": scenarios,
    }

    output_path.write_text(json.dumps(report, indent=2), encoding="utf-8")

    print(f"Wrote benchmark report to {output_path}")
    for s in scenarios:
        print(
            "size={size} throughput={tps} msg/s p50={p50}ms p95={p95}ms p99={p99}ms ai_cost=${cost}".format(
                size=s["message_count"],
                tps=s["throughput_msg_per_sec"],
                p50=s["latency_ms"]["p50"],
                p95=s["latency_ms"]["p95"],
                p99=s["latency_ms"]["p99"],
                cost=s["ai_cost"]["estimated"]["total_cost_usd"],
            )
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
