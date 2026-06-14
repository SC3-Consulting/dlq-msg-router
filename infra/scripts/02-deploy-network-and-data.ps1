param ([string]$Environment = "dev")
$ErrorActionPreference = "Stop"

# Use Linux-safe forward slashes
$RootDir = Resolve-Path "$PSScriptRoot/../.."
$TfDir = Join-Path $RootDir "infra/terraform/azure"
$BackendFile = Join-Path $TfDir "environments/$Environment/backend.hcl"
$BootstrapVarsFile = Join-Path $TfDir "environments/$Environment/bootstrap.generated.tfvars"

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
  "-var-file=$BootstrapVarsFile",
  "-var=environment=$Environment",
  "-var=location=australiaeast",
  "-var=resource_group_name=rg-viva-dlq-dev",
  "-var=vnet_cidr=10.20.0.0/16",
  "-var=private_endpoint_subnet_cidr=10.20.1.0/24",
  "-var=agent_subnet_cidr=10.20.2.0/24",
  "-var=container_apps_subnet_cidr=10.20.5.0/24",
  "-var=jumpbox_subnet_cidr=10.20.3.0/24",
  "-var=azure_bastion_subnet_cidr=10.20.4.0/24",
  "-var=jumpbox_vm_size=Standard_B2s"
)
& terraform $ApplyArgs
Write-Host "==> Phase 2 Complete. VNet, ACR, ASB, and Jumpbox are online." -ForegroundColor Green