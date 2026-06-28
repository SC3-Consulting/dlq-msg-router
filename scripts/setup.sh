#!/bin/bash

##########################
# setup.sh
# Sets up the Python virtual environment and installs dependencies for the DLQ Smart Triage Router project  
# Usage:
#   ./scripts/setup.sh
# Optional environment variables:
#   INSTALL_DIR  Install destination (default: /usr/local/bin)      
#
##########################

# Exit immediately if a command exits with a non-zero status
set -e

# ANSI colour codes for clean, professional terminal output
GREEN='\033[0;32m'
BLUE='\033[0;34m'
RED='\033[0;31m'
NC='\033[0m' # No Colour

echo -e "${BLUE}======================================================"
echo -e " DLQ Smart Triage Router - Infrastructure Setup"
echo -e "======================================================${NC}"

# 1. Dependency Check: Verify Python 3
if ! command -v python3 &> /dev/null; then
    echo -e "${RED}[ERROR] Python3 is not installed. Please install Python 3.10+${NC}"
    exit 1
fi

# 2. Virtual Environment Lifecycle
if [ ! -d ".venv" ]; then
    echo -e "${GREEN}[INFO] Creating Python virtual environment...${NC}"
    python3 -m venv .venv
else
    echo -e "${GREEN}[INFO] Virtual environment exists. Skipping creation.${NC}"
fi

# 3. Activation
echo -e "${GREEN}[INFO] Activating virtual environment...${NC}"
source .venv/bin/activate

# 4. Dependency Installation
echo -e "${GREEN}[INFO] Upgrading pip and installing dependencies...${NC}"
pip install --upgrade pip > /dev/null
pip install -r requirements.txt > /dev/null

# 5. Directory Structure Validation
# Using absolute paths to ensure we are in the correct root regardless of execution context
REQUIRED_DIRS=("data" "reports" "simulator" "src" "tests")

for dir in "${REQUIRED_DIRS[@]}"; do
    if [ ! -d "$dir" ]; then
        echo -e "${BLUE}[INFO] Creating missing directory: $dir${NC}"
        mkdir -p "$dir"
    fi
done

# 6. Environment Configuration
if [ ! -f ".env" ]; then
    echo -e "${RED}[WARN] .env file not found. Creating from .env.example...${NC}"
    if [ -f ".env.example" ]; then
        cp .env.example .env
        echo -e "${GREEN}[INFO] Please update your .env with your Azure/Ollama credentials.${NC}"
    fi
fi

echo -e "${BLUE}======================================================"
echo -e "${GREEN}[SUCCESS] Environment ready for execution.${NC}"
echo -e "Activation command: ${BLUE}source .venv/bin/activate${NC}"
echo -e "======================================================${NC}"