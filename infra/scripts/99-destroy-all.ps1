param ([string]$Environment = "dev")
$ErrorActionPreference = "Stop"

# Use Linux-safe forward slashes
$RootDir = Resolve-Path "$PSScriptRoot/../.."
$TfDir = Join-Path $RootDir "infra/terraform/azure"
$BootstrapVarsFile = Join-Path $TfDir "environments/$Environment/bootstrap.generated.tfvars"

Write-Host "==> DANGER: Destroying all ephemeral compute and network resources..." -ForegroundColor Red
Set-Location $TfDir

# Array method for Destroy
$DestroyArgs = @(
  "destroy",
  "-auto-approve",
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
& terraform $DestroyArgs

Write-Host "==> Teardown complete. Overnight costs have been mitigated." -ForegroundColor Green