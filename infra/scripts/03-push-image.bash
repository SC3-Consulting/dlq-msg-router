#!/bin/bash
set -e

# Run this INSIDE the Jumpbox
echo "==> Authenticating to ACR via Jumpbox Managed Identity..."
ACR_NAME="acrvivadlqswastik99" 
az login --identity
az acr login --name $ACR_NAME

echo "==> Building and Pushing DLQ Agent Image..."
# Assuming you cloned your repo into the jumpbox
cd ~/viva-dlq-agent
docker build -t ${ACR_NAME}.azurecr.io/viva-dlq-agent:v1.0.0 .
docker push ${ACR_NAME}.azurecr.io/viva-dlq-agent:v1.0.0

echo "==> Image Push Complete."