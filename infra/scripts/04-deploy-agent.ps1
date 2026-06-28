<#
.SYNOPSIS
    Deploys the agent for the specified environment.
.DESCRIPTION
    This script applies the Terraform module responsible for provisioning the agent hosting resources.
.PARAMETER Environment
    The target deployment environment (e.g., dev, test, prod). Defaults to 'dev'.
.EXAMPLE
    .\04-deploy-agent.ps1 -Environment dev
#>
param ([string]$Environment = "dev")
$ErrorActionPreference = "Stop"

# Use Linux-safe forward slashes
$RootDir = Resolve-Path "$PSScriptRoot/../.."
$TfDir = Join-Path $RootDir "infra/terraform/azure"
$BootstrapVarsFile = Join-Path $TfDir "environments/$Environment/bootstrap.generated.tfvars"
$PlatformVarsFile = Join-Path $TfDir "environments/$Environment/platform.tfvars"

if (-not (Test-Path $PlatformVarsFile)) {
  throw "Missing platform vars file: $PlatformVarsFile"
}

if (-not (Test-Path $BootstrapVarsFile)) {
  throw "Missing bootstrap vars file: $BootstrapVarsFile. Run 01-bootstrap.ps1 first."
}

Write-Host "==> Phase 3: Deploying agent from environment tfvars and Key Vault references..." -ForegroundColor Cyan

Write-Host "==> Deploying Agent Hosting Module..." -ForegroundColor Cyan
Set-Location $TfDir

# Array method for Apply
$ApplyArgs = @(
  "apply",
  "-auto-approve",
  "-target=module.agent_hosting",
  "-var-file=$PlatformVarsFile",
  "-var-file=$BootstrapVarsFile",
  "-var=environment=$Environment"
)
& terraform $ApplyArgs