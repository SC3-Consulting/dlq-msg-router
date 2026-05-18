import json
import requests
import time

OLLAMA_ENDPOINT = "http://localhost:11434/api/generate"
OLLAMA_MODEL = "llama-local" # CHANGE THIS TO YOUR ACTUAL LOCAL MODEL

def test_local_ai():
    print(f"--- Initiating Local AI Diagnostics ({OLLAMA_MODEL}) ---")
    
    # 1. The Mock Poison Pill Data (Our SAP Crash)
    reason = "UnhandledException"
    description = "java.net.ConnectException: Connection refused (Connection timed out) at com.sap.gateway.Sync.execute(Sync.java:42)"
    payload_str = '{"orderId": "ORD-77323", "clientId": "TRIGGER_SAP_CRASH", "amount": 890.00}'

    prompt = f"""
    You are an enterprise integration architect. Classify this Dead Letter Queue message.
    Reason: {reason}
    Description: {description}
    Payload: {payload_str}
    
    Output ONLY a raw JSON object matching this schema, nothing else. Do not use markdown code blocks.
    {{
        "classification": "string",
        "suggested_action": "string",
        "confidence": float
    }}
    """
    
    request_payload = {
        "model": OLLAMA_MODEL,
        "prompt": prompt,
        "stream": False,
        "format": "json" # This forces Ollama to output valid JSON
    }

    try:
        print("Sending prompt to Ollama... (This tests your GPU/CPU inference speed)")
        start_time = time.time()
        
        response = requests.post(OLLAMA_ENDPOINT, json=request_payload)
        response.raise_for_status()
        
        end_time = time.time()
        
        # 2. Extract and parse the response
        raw_output = response.json().get("response", "{}")
        print(f"\n[Raw LLM Output received in {round(end_time - start_time, 2)} seconds]:\n{raw_output}\n")
        
        # 3. Test the Parser (The "Bad JSON" paranoia check)
        parsed_json = json.loads(raw_output)
        print("✅ JSON Parsing Successful! The AI contract is solid.")
        print(f"Classification: {parsed_json.get('classification')}")
        print(f"Action:       {parsed_json.get('suggested_action')}")
        
    except requests.exceptions.ConnectionError:
        print("❌ ERROR: Connection Refused. Is Ollama running in WSL? (Run 'ollama serve' in a separate terminal)")
    except json.JSONDecodeError:
        print("❌ ERROR: The LLM hallucinated bad JSON. We will need to harden the prompt.")
    except Exception as e:
        print(f"❌ ERROR: {str(e)}")

if __name__ == "__main__":
    test_local_ai()