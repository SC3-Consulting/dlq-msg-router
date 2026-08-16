#!/usr/bin/env python3
"""Diagnose Azure Foundry behaviour from a jumpbox or local shell.

This script is intentionally narrow:
- It uses the same AzureFoundryEngine call path as the running app.
- It can also issue a direct ChatCompletionsClient call for comparison.
- It prints elapsed time and failure details so transient rate limit or empty
  response behaviour is easy to spot outside Container Apps.
"""

import argparse
import json
import logging
import os
import re
import subprocess
import sys
import time
from typing import Any, Dict

from azure.ai.inference import ChatCompletionsClient
from azure.ai.inference.models import SystemMessage, UserMessage
from azure.identity import DefaultAzureCredential

from src.ai_client import AzureFoundryEngine

LOGGER = logging.getLogger("foundry_diagnostic")


def _build_probe_payload(run_label: str) -> str:
    """Builds a synthetic payload for probing Azure Foundry.

    Args:
        run_label (str): A unique label to distinguish repeated probe runs.

    Returns:
        str: A JSON string representing the probe payload.
    """
    return json.dumps(
        {
            "transaction_amount": 50.0,
            "trigger_unknown_fault": True,
            "customer_email": "test.user@financial.com",
            "customer_phone": "+1234567890",
            "credit_card": "4111 1111 1111 1111",
            "run_label": run_label,
        }
    )


def _print_env_summary() -> None:
    """Prints a summary of relevant environment variables for diagnostics."""
    keys = [
        "AI_PROVIDER",
        "AZURE_FOUNDRY_ENDPOINT",
        "AZURE_FOUNDRY_DEPLOYMENT_NAME",
        "AZURE_FOUNDRY_MAX_TOKENS",
        "AZURE_FOUNDRY_EMPTY_RESPONSE_MAX_TOKENS",
        "AZURE_FOUNDRY_TRANSIENT_RETRIES",
        "AZURE_CLIENT_ID",
    ]
    print("[diag] Environment summary")
    for key in keys:
        print(f"[diag]   {key}={os.getenv(key, '')}")


def _extract_deployment_name(endpoint: str) -> str:
    """Extracts deployment name from a deployment-scoped Foundry endpoint."""
    if not endpoint:
        return ""
    match = re.search(r"/openai/deployments/([^/?]+)", endpoint)
    return match.group(1) if match else ""


def _resolve_foundry_env() -> None:
    """Resolves missing Foundry env vars from endpoint and Terraform outputs.

    Priority:
    1) Existing env vars.
    2) Parse deployment name from AZURE_FOUNDRY_ENDPOINT if deployment-scoped.
    3) Terraform output `foundry_endpoint` as a fallback source.
    """
    endpoint = os.getenv("AZURE_FOUNDRY_ENDPOINT", "").strip()
    deployment_name = os.getenv("AZURE_FOUNDRY_DEPLOYMENT_NAME", "").strip()

    if deployment_name:
        return

    parsed_name = _extract_deployment_name(endpoint)
    if parsed_name:
        os.environ["AZURE_FOUNDRY_DEPLOYMENT_NAME"] = parsed_name
        print(
            "[diag] Resolved AZURE_FOUNDRY_DEPLOYMENT_NAME from AZURE_FOUNDRY_ENDPOINT."
        )
        return

    try:
        tf_output = subprocess.check_output(
            [
                "terraform",
                "-chdir=infra/terraform/azure",
                "output",
                "-raw",
                "foundry_endpoint",
            ],
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=10,
        ).strip()
    except (
        subprocess.CalledProcessError,
        FileNotFoundError,
        subprocess.TimeoutExpired,
    ):
        tf_output = ""

    if not tf_output:
        return

    if not endpoint:
        os.environ["AZURE_FOUNDRY_ENDPOINT"] = tf_output
        endpoint = tf_output
        print("[diag] Resolved AZURE_FOUNDRY_ENDPOINT from Terraform output.")

    parsed_name = _extract_deployment_name(tf_output) or _extract_deployment_name(
        endpoint
    )
    if parsed_name:
        os.environ["AZURE_FOUNDRY_DEPLOYMENT_NAME"] = parsed_name
        print("[diag] Resolved AZURE_FOUNDRY_DEPLOYMENT_NAME from Terraform output.")


def _verify_token() -> None:
    """Verifies that the credential can acquire a Cognitive Services token."""
    print("[diag] Verifying credential can acquire Cognitive Services token...")
    start = time.monotonic()
    credential = DefaultAzureCredential()
    token = credential.get_token("https://cognitiveservices.azure.com/.default")
    elapsed = time.monotonic() - start
    print(f"[diag] Token acquired in {elapsed:.2f}s; expires_on={token.expires_on}")


def _run_engine_call(client_id: str, run_label: str) -> int:
    """Runs a call to the AzureFoundryEngine with a synthetic payload.

    Args:
        client_id (str): The client ID for the Azure Foundry engine.
        run_label (str): A unique label to distinguish repeated probe runs.

    Returns:
        int: The exit code of the operation.
    """
    print("[diag] Running AzureFoundryEngine.call_llm...")
    engine = AzureFoundryEngine()
    start = time.monotonic()
    result = engine.call_llm(
        client_id=client_id,
        reason="SystemFault",
        description="Unexpected null pointer",
        payload=_build_probe_payload(run_label),
    )
    elapsed = time.monotonic() - start
    print(f"[diag] Engine call succeeded in {elapsed:.2f}s")
    print(json.dumps(result, indent=2, sort_keys=True))
    return 0


def _describe_content_shape(content: Any) -> str:
    """Returns a compact description of the response content shape."""
    if content is None:
        return "None"
    if isinstance(content, str):
        return f"str(len={len(content)})"
    if isinstance(content, list):
        part_types = [type(item).__name__ for item in content]
        return f"list(len={len(content)}, item_types={part_types})"
    return type(content).__name__


def _print_response_diagnostics(response: Any) -> None:
    """Prints non-content response diagnostics for troubleshooting empty outputs."""
    choice = response.choices[0] if getattr(response, "choices", None) else None
    if not choice:
        return

    finish_reason = getattr(choice, "finish_reason", None)
    if finish_reason is not None:
        print(f"[diag] Choice finish_reason: {finish_reason}")

    content_filter_results = getattr(choice, "content_filter_results", None)
    if content_filter_results is not None:
        print(f"[diag] Choice content_filter_results: {content_filter_results}")

    usage = getattr(response, "usage", None)
    if usage is not None:
        print(f"[diag] Usage: {usage}")

    # Best-effort extraction in case the SDK model has additional fields not
    # exposed as strongly-typed attributes.
    as_dict = getattr(response, "as_dict", None)
    if callable(as_dict):
        try:
            raw_dict = as_dict()
            choices = raw_dict.get("choices") if isinstance(raw_dict, dict) else None
            if isinstance(choices, list) and choices:
                first = choices[0]
                if isinstance(first, dict):
                    for key in [
                        "finish_reason",
                        "content_filter_results",
                        "message",
                    ]:
                        if key in first:
                            print(f"[diag] Choice[{key}] from as_dict: {first[key]}")
        except Exception:
            pass


def _run_raw_call(client_id: str, run_label: str, force_json_object: str) -> int:
    print("[diag] Running direct ChatCompletionsClient.complete...")
    engine = AzureFoundryEngine()
    messages = [
        SystemMessage(
            content=f"""You are an operations support engineer managing an Azure Service Bus environment.
A message has fallen into the Dead Letter Queue (DLQ).
Analyse the raw payload to deduce why it failed and recommend how to handle it.

--- INSTRUCTIONS ---
You must output YOUR ENTIRE RESPONSE as a single, valid JSON object. Do not include conversational text."""
        ),
        UserMessage(
            content=engine._build_prompt(  # Intentional reuse of the app prompt.
                client_id=client_id,
                reason="SystemFault",
                description="Unexpected null pointer",
                payload=_build_probe_payload(run_label),
                is_truncated=False,
            )
        ),
    ]

    client = ChatCompletionsClient(
        endpoint=engine.endpoint,
        credential=DefaultAzureCredential(),
        credential_scopes=["https://cognitiveservices.azure.com/.default"],
    )

    kwargs: Dict[str, Any] = {
        "messages": messages,
        "model": engine.deployment_name,
    }
    if force_json_object == "on":
        kwargs["response_format"] = "json_object"
    if engine.temperature is not None:
        kwargs["temperature"] = engine.temperature

    start = time.monotonic()
    temperature_removed = False
    use_model_extras = False
    while True:
        try:
            if use_model_extras:
                response = client.complete(
                    model_extras={"max_completion_tokens": engine.max_tokens},
                    **kwargs,
                )
            else:
                response = client.complete(max_tokens=engine.max_tokens, **kwargs)
            break
        except Exception as exc:
            message = str(exc)
            message_lower = message.lower()
            call_mode = (
                "model_extras.max_completion_tokens"
                if use_model_extras
                else "max_tokens"
            )
            print(f"[diag] Raw call with {call_mode} failed: {message}")

            if (
                not use_model_extras
                and "max_completion_tokens" in message
                and "max_tokens" in message
            ):
                use_model_extras = True
                print(
                    "[diag] Retrying raw call with model_extras.max_completion_tokens..."
                )
                continue

            if (
                "temperature" in kwargs
                and "unsupported_value" in message_lower
                and "temperature" in message_lower
            ):
                kwargs.pop("temperature", None)
                temperature_removed = True
                print("[diag] Retrying raw call without explicit temperature...")
                continue

            raise

    elapsed = time.monotonic() - start
    print(f"[diag] Raw call returned in {elapsed:.2f}s")
    if temperature_removed:
        print("[diag] Temperature fallback path was exercised.")
    if not response.choices:
        print("[diag] No choices returned.")
        return 2

    _print_response_diagnostics(response)
    content = response.choices[0].message.content
    print(f"[diag] Raw content shape: {_describe_content_shape(content)}")
    print("[diag] Raw model content:")
    print(content if content else "<empty>")
    return 0


def main() -> int:
    """Main entry point for the Azure Foundry diagnostic script."""
    parser = argparse.ArgumentParser(
        description="Probe Azure Foundry from jumpbox or shell using the same SDK path as the app."
    )
    parser.add_argument(
        "--mode",
        choices=["engine", "raw", "both"],
        default="both",
        help="Which probe path to run.",
    )
    parser.add_argument(
        "--client-id",
        default="Omega_Corp_diag",
        help="Client identifier label used in the probe prompt.",
    )
    parser.add_argument(
        "--run-label",
        default=f"diag-{int(time.time())}",
        help="Unique label to distinguish repeated probe runs.",
    )
    parser.add_argument(
        "--raw-force-json-object",
        choices=["on", "off"],
        default="on",
        help="For --mode raw/both, force response_format=json_object or disable it.",
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s - %(levelname)s - [%(threadName)s] - %(message)s",
    )

    if os.getenv("AI_PROVIDER", "").upper() != "AZURE_FOUNDRY":
        os.environ["AI_PROVIDER"] = "AZURE_FOUNDRY"

    _resolve_foundry_env()
    _print_env_summary()

    try:
        _verify_token()

        if args.mode in {"engine", "both"}:
            _run_engine_call(args.client_id, args.run_label)

        if args.mode in {"raw", "both"}:
            _run_raw_call(
                args.client_id,
                args.run_label,
                args.raw_force_json_object,
            )

        return 0
    except Exception as exc:
        print(f"[diag] FAILED: {type(exc).__name__}: {exc}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
