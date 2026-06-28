#!/bin/bash

##########################
# 03-push-image.bash
# Builds and pushes the Message Router Agent Docker image to ACR
#
# Usage:
#   ./infra/scripts/03-push-image.bash
#########################

set -e

# Run this INSIDE the Jumpbox
echo "==> Authenticating to ACR via Jumpbox Managed Identity..."
ACR_NAME="acrmsgrouter99"
az login --identity
az acr login --name $ACR_NAME

echo "==> Building and Pushing Message Router Agent Image..."
# Assuming you cloned your repo into the jumpbox
cd ~/message-router-agent
docker build -t ${ACR_NAME}.azurecr.io/router-agent:v1.0.0 .
docker push ${ACR_NAME}.azurecr.io/router-agent:v1.0.0

echo "==> Image Push Complete."