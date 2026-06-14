<#
.SYNOPSIS
    Bootstraps the remote Terraform state for the Autonomous DLQ Triage Pipeline.

.DESCRIPTION
    This script provisions the foundational Azure resources required to securely execute Terraform.
    It creates an Azure Storage Account to hold the .tfstate file and an Azure Key Vault to 
    securely store the Jumpbox SSH keys. It then dynamically generates the backend.hcl configuration 
    so subsequent Terraform phases know where to store their state.

.PARAMETER Environment
    The target deployment environment (e.g., dev, test, prod). Defaults to 'dev'.

.PARAMETER Location
    The Azure region to deploy the bootstrap resources into. Defaults to 'australiaeast'.

.EXAMPLE
    .\01-bootstrap.ps1 -Environment dev
#>

param (
    [string]$Environment = "dev",
    [string]$Location = "australiaeast"
)

$ErrorActionPreference = "Stop"

# Validate environment
$ValidEnvironments = @("dev", "test", "prod")
if ($Environment -notin $ValidEnvironments) {
    Write-Error "Unsupported environment '$Environment'. Must be one of: dev, test, prod."
    exit 1
}

# Define Paths
$RootDir = Resolve-Path (Join-Path $PSScriptRoot "..\..")
$BootstrapDir = Join-Path $RootDir "infra\terraform\azure\bootstrap"
$EnvDir = Join-Path $RootDir "infra\terraform\azure\environments\$Environment"
$BackendFile = Join-Path $EnvDir "backend.hcl"
$GeneratedVarsFile = Join-Path $EnvDir "bootstrap.generated.tfvars"

# Ensure target environment directory exists
if (-not (Test-Path $EnvDir)) {
    New-Item -ItemType Directory -Force -Path $EnvDir | Out-Null
}

# Pre-flight Checks
Write-Host "==> Performing pre-flight dependency checks..." -ForegroundColor Cyan
if (-not (Get-Command "terraform" -ErrorAction SilentlyContinue)) {
    Write-Error "Terraform CLI is not installed or not in PATH."
    exit 1
}

if (-not (Get-Command "az" -ErrorAction SilentlyContinue)) {
    Write-Error "Azure CLI is not installed or not in PATH."
    exit 1
}

# Verify Azure Authentication
try {
    $null = az account show
} catch {
    Write-Error "Azure CLI is not authenticated. Please run 'az login' before executing this script."
    exit 1
}

# Register Required Azure Resource Providers
Write-Host "==> Registering required Azure resource providers..." -ForegroundColor Cyan
$Providers = @(
    "Microsoft.App",
    "Microsoft.Monitor",
    "Microsoft.CognitiveServices",
    "Microsoft.KeyVault",
    "Microsoft.ServiceBus",
    "Microsoft.Compute",
    "Microsoft.ContainerRegistry"
)

foreach ($Provider in $Providers) {
    Write-Host "    Requesting registration for $Provider..."
    az provider register --namespace $Provider | Out-Null
}

# Async wait for provider registration (Azure Management Plane delay)
Write-Host "==> Waiting for provider registrations to finalize (this may take a few minutes)..." -ForegroundColor Cyan
foreach ($Provider in $Providers) {
    $Status = "Registering"
    while ($Status -ne "Registered") {
        $Status = (az provider show --namespace $Provider --query registrationState -o tsv).Trim()
        if ($Status -eq "Registered") {
            Write-Host "    ${Provider}: Registered" -ForegroundColor Green
        } else {
            Start-Sleep -Seconds 5
        }
    }
}

# Define Terraform Variables
$ResourceGroupName = "rg-viva-dlq-dev"
$StorageAccountPrefix = "sttfstate$Environment"
$KeyVaultPrefix = "kvtfstate"
$BackendKey = "platform/${Environment}.tfstate"

Write-Host "==> Initialising bootstrap Terraform stack..." -ForegroundColor Cyan
Set-Location $BootstrapDir
terraform init -upgrade

Write-Host "==> Applying bootstrap Terraform stack..." -ForegroundColor Cyan
# Execute Terraform Apply. 
terraform apply -auto-approve `
    -input=false `
    -parallelism=1 `
    -lock-timeout=5m `
    -var="location=$Location" `
    -var="resource_group_name=$ResourceGroupName" `
    -var="storage_account_name_prefix=$StorageAccountPrefix" `
    -var="enable_bootstrap_key_vault=true" `
    -var="key_vault_name_prefix=$KeyVaultPrefix"

Write-Host "==> Reading generated Azure resource outputs..." -ForegroundColor Cyan
$StateRg = terraform output -raw resource_group_name
$StateSa = terraform output -raw storage_account_name
$StateContainer = terraform output -raw container_name
$StateKeyVault = terraform output -raw key_vault_name

# Generate backend.hcl for subsequent Terraform phases
Write-Host "==> Generating backend configuration: $BackendFile" -ForegroundColor Cyan
$BackendContent = @"
resource_group_name  = "$StateRg"
storage_account_name = "$StateSa"
container_name       = "$StateContainer"
key                  = "$BackendKey"
use_azuread_auth     = true
"@
Set-Content -Path $BackendFile -Value $BackendContent -Force

# Generate bootstrap.generated.tfvars to pass the Key Vault name to Phase 2
Write-Host "==> Generating bootstrap variables: $GeneratedVarsFile" -ForegroundColor Cyan
$VarsContent = @"
bootstrap_key_vault_name = "$StateKeyVault"
bootstrap_key_vault_resource_group_name = "$StateRg"
jumpbox_ssh_public_key_secret_name = "jumpbox-admin-ssh-public-key-$Environment"
"@
Set-Content -Path $GeneratedVarsFile -Value $VarsContent -Force

Write-Host "------------------------------------------------------" -ForegroundColor Green
Write-Host "Phase 1 Bootstrap Completed Successfully." -ForegroundColor Green
Write-Host "Storage Account : $StateSa" -ForegroundColor Green
Write-Host "Key Vault       : $StateKeyVault" -ForegroundColor Green
Write-Host "------------------------------------------------------" -ForegroundColor Green

# Return to original execution directory
Set-Location $PSScriptRoot