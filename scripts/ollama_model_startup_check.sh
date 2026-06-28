#!/usr/bin/env bash

##########################
# ollama_model_startup_check.sh
# Checks the system's GPU VRAM and provides a recommended Ollama model for startup.
# Only supports Linux with NVIDIA GPUs. If no GPU is detected, it defaults to a CPU-safe recommendation.
# Usage:
#   ./scripts/ollama_model_startup_check.sh
#
# Optional environment variables:
#   OLLAMA_MODEL  Specify the Ollama model to use (overrides automatic detection)
##########################

set -euo pipefail

is_docker="false"
is_wsl="false"

if [[ -f /.dockerenv ]] || grep -qaE "docker|containerd|kubepods" /proc/1/cgroup 2>/dev/null; then
  is_docker="true"
fi

if [[ -n "${WSL_DISTRO_NAME:-}" ]] || grep -qi "microsoft" /proc/version 2>/dev/null; then
  is_wsl="true"
fi

print_cpu_safe() {
  cat <<'EOF'
Ollama model startup recommendation
- CPU-safe default: qwen2.5:0.5b
- CPU alternative:  llama3.2:1b

To apply:
export OLLAMA_MODEL="qwen2.5:0.5b"
EOF
}

if [[ "${is_docker}" == "true" ]] && [[ "${is_wsl}" == "true" ]]; then
  cat <<'EOF'
[WARN] Running in Docker within WSL.
GPU/VRAM detection may be inaccurate when Ollama runs in a different runtime boundary.
Use this script output as guidance only, and set OLLAMA_MODEL explicitly if needed.
EOF
elif [[ "${is_docker}" == "true" ]]; then
  cat <<'EOF'
[WARN] Running inside a container.
GPU/VRAM detection may not reflect the host GPU unless NVIDIA runtime passthrough is configured.
Use this script output as guidance only.
EOF
elif [[ "${is_wsl}" == "true" ]]; then
  cat <<'EOF'
[INFO] Running in WSL.
Detection usually works, but can differ from where Ollama actually runs.
EOF
fi

if ! command -v nvidia-smi >/dev/null 2>&1; then
  echo "[INFO] nvidia-smi not found. Falling back to CPU-safe recommendation."
  print_cpu_safe
  exit 0
fi

vram_mb_raw="$(nvidia-smi --query-gpu=memory.total --format=csv,noheader,nounits 2>/dev/null | head -n1 | tr -d '[:space:]')"
if [[ -z "${vram_mb_raw}" ]] || ! [[ "${vram_mb_raw}" =~ ^[0-9]+$ ]]; then
  echo "[WARN] Could not determine GPU VRAM from nvidia-smi."
  print_cpu_safe
  exit 0
fi

vram_mb="${vram_mb_raw}"
vram_gb="$((vram_mb / 1024))"

echo "Detected GPU VRAM: ${vram_mb} MB (~${vram_gb} GB)"

if (( vram_mb >= 16384 )); then
  model="qwen2.5:14b-instruct-q4_K_M"
  tier=">= 16 GB VRAM"
elif (( vram_mb >= 8192 )); then
  model="qwen2.5:7b-instruct"
  alt_model="llama3.1:8b-instruct-q4_K_M"
  tier=">= 8 GB VRAM"
else
  model="qwen2.5:0.5b"
  alt_model="llama3.2:1b"
  tier="< 8 GB VRAM"
fi

echo "Ollama model startup recommendation"
echo "- Tier: ${tier}"
echo "- Recommended: ${model}"
if [[ -n "${alt_model:-}" ]]; then
  echo "- Alternative: ${alt_model}"
fi

echo
echo "To apply:"
echo "export OLLAMA_MODEL=\"${model}\""
