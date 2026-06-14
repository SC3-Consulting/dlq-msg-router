param ([string]$Environment = "dev")
$ErrorActionPreference = "Stop"

# Use Linux-safe forward slashes
$RootDir = Resolve-Path "$PSScriptRoot/../.."
$TfDir = Join-Path $RootDir "infra/terraform/azure"
$BootstrapVarsFile = Join-Path $TfDir "environments/$Environment/bootstrap.generated.tfvars"
$EnvFile = Join-Path $RootDir ".env"

Write-Host "==> Phase 3: Parsing .env file..." -ForegroundColor Cyan
$EnvVars = @{}
foreach($line in Get-Content $EnvFile) {
    if ($line -match "^([^#=]+)=(.*)$") {
        $key = $matches[1].Trim()
        # Trim whitespace and remove surrounding double quotes
        $val = $matches[2].Trim().Trim('"')
        $EnvVars[$key] = $val
    }
}
$EnvJson = $EnvVars | ConvertTo-Json -Compress
$env:TF_VAR_app_env_vars = $EnvJson

Write-Host "==> Deploying Agent Hosting Module..." -ForegroundColor Cyan
Set-Location $TfDir

# Array method for Apply
$ApplyArgs = @(
  "apply",
  "-auto-approve",
  "-target=module.agent_hosting",
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