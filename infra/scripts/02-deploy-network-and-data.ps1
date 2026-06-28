<#
.SYNOPSIS
    Deploys the network and data plane for the specified environment.
.DESCRIPTION
    This script applies the Terraform modules responsible for provisioning the foundational network and data services.
.PARAMETER Environment
    The target deployment environment (e.g., dev, test, prod). Defaults to 'dev'.
.EXAMPLE
    .\02-deploy-network-and-data.ps1 -Environment dev   
#>
param ([string]$Environment = "dev")
$ErrorActionPreference = "Stop"

# Use Linux-safe forward slashes
$RootDir = Resolve-Path "$PSScriptRoot/../.."
$TfDir = Join-Path $RootDir "infra/terraform/azure"
$BackendFile = Join-Path $TfDir "environments/$Environment/backend.hcl"
$BootstrapVarsFile = Join-Path $TfDir "environments/$Environment/bootstrap.generated.tfvars"
$PlatformVarsFile = Join-Path $TfDir "environments/$Environment/platform.tfvars"

Write-Host "==> Phase 2: Deploying Network & Data Plane..." -ForegroundColor Cyan
Set-Location $TfDir

# Array method for Init (prevents literal string parsing bugs)
$InitArgs = @(
  "init",
  "-reconfigure",
  "-backend-config=$BackendFile"
)
& terraform $InitArgs

# Array method for Apply (prevents backtick/spacing bugs and includes module.dns)
$ApplyArgs = @(
  "apply",
  "-auto-approve",
  "-target=module.foundation",
  "-target=module.network",
  "-target=module.dns",
  "-target=module.observability",
  "-target=module.data_services",
  "-target=module.foundry",
  "-target=module.private_endpoints",
  "-target=module.identity",
  "-target=module.bastion_jumpbox",
  "-var-file=$PlatformVarsFile",
  "-var-file=$BootstrapVarsFile",
  "-var=environment=$Environment"
)
& terraform $ApplyArgs
Write-Host "==> Phase 2 Complete. VNet, ACR, ASB, and Jumpbox are online." -ForegroundColor Green