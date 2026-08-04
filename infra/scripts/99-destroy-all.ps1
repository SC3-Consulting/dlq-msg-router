<#
.SYNOPSIS
    Destroys all resources for the specified environment.
.DESCRIPTION
    This script applies the Terraform destroy command to remove all resources for the specified environment.
.PARAMETER Environment
    The target deployment environment (e.g., dev, test, prod). Defaults to 'dev'.
.EXAMPLE
    .\99-destroy-all.ps1 -Environment dev
#>
param ([string]$Environment = "dev")
$ErrorActionPreference = "Stop"

# Use Linux-safe forward slashes
$RootDir = Resolve-Path "$PSScriptRoot/../.."
$TfDir = Join-Path $RootDir "infra/terraform/azure"
$BootstrapVarsFile = Join-Path $TfDir "environments/$Environment/bootstrap.generated.tfvars"
$PlatformVarsFile = Join-Path $TfDir "environments/$Environment/platform.tfvars"

Write-Host "==> DANGER: Destroying all ephemeral compute and network resources..." -ForegroundColor Red
Set-Location $TfDir

# Array method for Destroy
$DestroyArgs = @(
  "destroy",
  "-auto-approve",
  "-var-file=$PlatformVarsFile",
  "-var-file=$BootstrapVarsFile",
  "-var=environment=$Environment"
)
& terraform $DestroyArgs

Write-Host "==> Teardown complete. Overnight costs have been mitigated." -ForegroundColor Green