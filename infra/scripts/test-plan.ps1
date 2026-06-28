<#
.SYNOPSIS
    Runs a Terraform plan (dry run) for the specified environment.
.DESCRIPTION
    This script applies the Terraform plan command to preview changes for the specified environment.
.PARAMETER Environment
    The target deployment environment (e.g., dev, test, prod). Defaults to 'dev'.
.EXAMPLE
    .\test-plan.ps1 -Environment dev 
#>

param ([string]$Environment = "dev")
$ErrorActionPreference = "Stop"

# Use Linux-native forward slashes (/) for WSL compatibility
$RootDir = Resolve-Path "$PSScriptRoot/../.."
$TfDir = Join-Path $RootDir "infra/terraform/azure"
$BackendFile = Join-Path $TfDir "environments/$Environment/backend.hcl"
$BootstrapVarsFile = Join-Path $TfDir "environments/$Environment/bootstrap.generated.tfvars"
$PlatformVarsFile = Join-Path $TfDir "environments/$Environment/platform.tfvars"

Write-Host "==> Running Terraform Plan (Dry Run)..." -ForegroundColor Cyan
Set-Location $TfDir

# Array method for Init (prevents literal string parsing bugs)
$InitArgs = @(
  "init",
  "-reconfigure",
  "-backend-config=$BackendFile"
)
& terraform $InitArgs

# Array method for Plan
$TfArgs = @(
  "plan",
  "-target=module.foundation",
  "-target=module.network",
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
& terraform $TfArgs