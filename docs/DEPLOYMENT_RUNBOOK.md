# Infrastructure Deployment Runbook: Autonomous DLQ Agent

This document outlines the step-by-step procedures for provisioning the secure, Zero Trust infrastructure for the Autonomous DLQ Triage Pipeline via Terraform. It serves as a comprehensive, zero-assumption guide designed to take an operator from a blank terminal to a fully operational, AI-augmented triage agent in Azure. 

Every command, operational trap, and environmental patch required for a flawless execution is documented below.

## Architectural Context & Permission Constraints

To adhere to strict enterprise security mandates, the deployment is orchestrated via PowerShell wrappers. This ensures secure injection of CLI credentials without exposing sensitive variables to version control. 

**The Resource Group Strategy:** Initially, separating the remote state storage (Storage Account and Key Vault) into a dedicated resource group is best practice. However, due to tenant-level 'User Access Administrator' restrictions preventing cross-resource-group role assignments, all infrastructure—including the state bootstrap—must be encapsulated within a pre-existing resource group (e.g., 'rg-viva-dlq-dev') where 'Owner' permissions are already established. This allows Terraform to autonomously execute the required 'Storage Blob Data Contributor' and 'Key Vault Secrets Officer' role assignments without elevated tenant approvals.

---

## Phase 0: Pre-flight Preparation & Authentication

Before initiating the deployment, authenticate the local terminal and prepare the secure SSH keys required for jumpbox access.

**Architectural Note (The Ephemeral Storage Bypass):** Azure Container Apps utilise ephemeral disks. Attempting to extract physical CSV telemetry files triggers an 'AuthorizationFailed' RBAC error. To bypass this securely, the repository's codebase (`src/run_agent.py`) is already pre-configured to broadcast telemetry directly to Azure Log Analytics via standard output. It intercepts the telemetry array and prints it using the following format: `print(f"CSV_EXPORT|{','.join(map(str, row))}")`. No manual code patching is required.

1. Authenticate with Azure using the device code flow for secure, headless login:

```bash
az login --use-device-code
```

2. Generate a local SSH key pair to securely access the Azure Bastion Jumpbox. Do not set a passphrase when prompted:

```bash
ssh-keygen -m PEM -t rsa -b 4096 -f ~/.ssh/viva_jumpbox_rsa -N ""
```

3. **Trap Resolution (Soft-Deleted Vaults):** Azure retains Key Vaults in a soft-deleted state, blocking recreation. If a previous deployment was torn down, purge the old vault manually before proceeding. Replace the name with your specific vault target:

[PUT TRIPLE BACK TICKS HERE]bash
az keyvault purge --name kvtfstatelogia7 --location australiaeast
[PUT TRIPLE BACK TICKS HERE]

---

## Phase 1: Remote State Bootstrap

**Objective:** Provision the foundational Azure resources required to securely execute Terraform. This creates the Storage Account for the '.tfstate' file and the Key Vault.

1. Execute the Phase 1 PowerShell orchestrator from your local repository root:

[PUT TRIPLE BACK TICKS HERE]powershell
pwsh ./infra/scripts/01-bootstrap.ps1
[PUT TRIPLE BACK TICKS HERE]

2. **Secure Key Injection:** Once the Key Vault is provisioned, manually read the generated public key and inject it into the vault as a secret. Execute these commands in your local PowerShell terminal:

[PUT TRIPLE BACK TICKS HERE]powershell
$PubKey = Get-Content ~/.ssh/viva_jumpbox_rsa.pub -Raw
az keyvault secret set --vault-name kvtfstatelogia7 --name jumpbox-admin-ssh-public-key-dev --value "$PubKey"
[PUT TRIPLE BACK TICKS HERE]

3. Initialise the Terraform backend memory to link the local workspace to the newly created Storage Account:

[PUT TRIPLE BACK TICKS HERE]bash
cd infra/terraform/azure
terraform init -reconfigure -backend-config=environments/dev/backend.hcl
cd ../../..
[PUT TRIPLE BACK TICKS HERE]

---

## Phase 2: Network & Data Plane Deployment

**Objective:** Deploy the Virtual Network, Private Endpoints, Azure Container Registry (ACR), Premium Service Bus, and the Azure Foundry cognitive accounts.

1. Execute the Phase 2 orchestrator:

[PUT TRIPLE BACK TICKS HERE]powershell
pwsh ./infra/scripts/02-deploy-network-and-data.ps1
[PUT TRIPLE BACK TICKS HERE]

2. Commit the structural changes to version control to ensure the remote repository is prepared for the jumpbox pull:

[PUT TRIPLE BACK TICKS HERE]bash
git add .
git commit -m "Phase 2: IaC modules, runbooks, and strict repo cleanup"
git push
[PUT TRIPLE BACK TICKS HERE]

---

## Phase 3: Jumpbox Provisioning & The Split-Brain Fix

**Objective:** Securely breach the private network via Azure Bastion, configure the bare-metal Jumpbox, inject the environment variables, and push the Docker image.

### 1. Breach the Jumpbox
1. Navigate to the Azure Portal.
2. Open 'vm-jumpbox-dev' and select 'Connect' -> 'Bastion'.
3. Username: 'azureuser'
4. Authentication Type: 'SSH Private Key from Local File'.
5. Upload the private key ('~/.ssh/viva_jumpbox_rsa' - select the file WITHOUT the '.pub' extension).
6. Uncheck 'Open in new browser tab', then click 'Connect'.

### 2. Provision the Bare-Metal Environment
The Jumpbox is a naked Ubuntu instance. Paste the following block directly into the Bastion terminal to install dependencies, clone the code, and bypass PEP 668 restrictions. Replace 'YOUR_GITHUB_USERNAME' with the actual username.

[PUT TRIPLE BACK TICKS HERE]bash
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
sudo apt-get update
sudo apt-get install -y docker.io python3-pip python3-venv
sudo chmod 666 /var/run/docker.sock
git clone https://github.com/YOUR_GITHUB_USERNAME/viva-dlq-agent.git
cd viva-dlq-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
[PUT TRIPLE BACK TICKS HERE]

### 3. The Split-Brain Environment Patch
**Trap Resolution:** The cloud agent dynamically discovers queues, whilst the Jumpbox simulators default to reading local JSON files. If a queue name has a typo in the local JSON, the simulators will crash with an 'amqp:not-found' error. 

To synchronise the Jumpbox with the production logic, the simulators must be forced to hit the Service Bus API. Paste this exact configuration into the Jumpbox terminal to create the '.env' file:

[PUT TRIPLE BACK TICKS HERE]bash
cat << 'EOF' > .env
# Azure Service Bus Configuration
SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE="sb-viva-dlq-swastik-99.servicebus.windows.net"

# Toggle for RBAC-secured dynamic discovery of queues
ENABLE_DYNAMIC_DISCOVERY="True"
# Comma-separated list of queues to ignore during dynamic discovery
EXCLUDED_QUEUES="parking-lot-queue,system-queue,archive-queue"

# Fallback JSON Array if Dynamic Discovery is disabled or hits an RBAC 403 error
ASB_SOURCES_FILE="data/asb_sources.json"
PARKING_LOT_QUEUE_NAME="parking-lot-queue"

# Maximum number of concurrent queues processed at any given second
MAX_CONCURRENT_QUEUES=5
ASB_MAX_MESSAGE_COUNT=10
ASB_MAX_WAIT_TIME=5
# Fills local RAM buffer to eliminate network latency during batch pulls
PREFETCH_COUNT=20


# Action Execution Feature Flags
ENABLE_NEW_MESSAGE_ID_ON_RETRY="True"

# Cache & Threshold Configuration
IDEMPOTENCY_TTL_SECONDS=86400
CLASSIFICATION_TTL_SECONDS=600
MAX_RESUBMIT_COUNT=3
DUPLICATE_NOISE_THRESHOLD=10

# Storage Paths
TELEMETRY_CSV_PATH="reports/telemetry_dashboard.csv"
IDEMPOTENCY_DB_PATH="data/idempotency.db"

# AI Client Configuration
OLLAMA_MODEL="qwen2.5:0.5b"
OLLAMA_ENDPOINT="http://localhost:11434/api/generate"
OLLAMA_TEMPERATURE=0.1
OLLAMA_NUM_CTX=4096
OLLAMA_TIMEOUT=240

# File Paths
RULES_FILE_PATH="data/rules.json"


# Azure Foundry Specifics (Required if AI_PROVIDER="AZURE_FOUNDRY")
AI_PROVIDER="AZURE_FOUNDRY"
AZURE_FOUNDRY_ENDPOINT="https://foundry-viva-swastik-99.cognitiveservices.azure.com/openai/deployments/gpt-4o-mini"
AZURE_FOUNDRY_DEPLOYMENT_NAME="gpt-4o-mini"
AZURE_FOUNDRY_TEMPERATURE=0.1
# Time to sleep (in seconds) after completing a full sweep of all queues
# Prevents aggressive AMQP link churn if all DLQs are empty.
AGENT_CYCLE_SLEEP_SECONDS=61
# Dynamic Cost Controls (Your Real-Time Airbags)
AZURE_FOUNDRY_MAX_TOKENS=300
EOF
[PUT TRIPLE BACK TICKS HERE]

### 4. Build and Push the Image
Execute the bash script to compile and push the container to the ACR using the Jumpbox's Managed Identity:

[PUT TRIPLE BACK TICKS HERE]bash
bash ./infra/scripts/03-push-image.bash
[PUT TRIPLE BACK TICKS HERE]

---

## Phase 4: Agent Hosting Deployment & Code Patching

**Objective:** Deploy the agent to Azure Container Apps and bypass ephemeral storage restrictions.

### 1. Trap Resolution: The Identity Crash
When mapping User-Assigned Managed Identities into Container Apps, the Python 'DefaultAzureCredential' defaults to System-Assigned, causing a fatal 'ClientAuthenticationError'. The Terraform configuration automatically resolves this by querying the exact 'client_id' and injecting it as 'AZURE_CLIENT_ID'.

### 2. Trap Resolution: The Static Tag Trap
Terraform will ignore the newly pushed image because the ACR tag remains statically set to 'v1.0.0'. To force Azure to pull the new code and generate a new deployment revision, modify a benign environment variable in the local `.env` file used by Terraform.

Change the sleep timer value from `60` to `61`:
[PUT TRIPLE BACK TICKS HERE]bash
AGENT_CYCLE_SLEEP_SECONDS=61
[PUT TRIPLE BACK TICKS HERE]

### 3. Execute Deployment
Return to the **local laptop terminal** and deploy the agent. Terraform will detect the altered `AGENT_CYCLE_SLEEP_SECONDS` variable and force a fresh container deployment.

[PUT TRIPLE BACK TICKS HERE]powershell
pwsh ./infra/scripts/04-deploy-agent.ps1
[PUT TRIPLE BACK TICKS HERE]

---

## Phase 5: Live Fire Simulation

**Objective:** Inject synthetic enterprise traffic to validate schema auto-healing, duplicate dropping, and Azure Foundry fallback.

**Trap Resolution (Module Execution):** Running Python scripts directly via path triggers a 'ModuleNotFoundError' due to execution isolation. They must be executed as modules from the repository root.

1. Open **two separate Bastion terminals** connected to the Jumpbox.
2. In BOTH terminals, activate the virtual environment:

[PUT TRIPLE BACK TICKS HERE]bash
cd ~/viva-dlq-agent
source .venv/bin/activate
[PUT TRIPLE BACK TICKS HERE]

3. **Terminal 1 (Sanitise the Environment):** Flush all ghost messages from the Service Bus:

[PUT TRIPLE BACK TICKS HERE]bash
python src/flush_queues.py
[PUT TRIPLE BACK TICKS HERE]

4. **Terminal 1 (The Consumer):** Start the downstream application simulator:

[PUT TRIPLE BACK TICKS HERE]bash
python -m simulator.consumer
[PUT TRIPLE BACK TICKS HERE]

5. **Terminal 2 (The Payload Cannon):** Dispatch the synthetic anomalies:

[PUT TRIPLE BACK TICKS HERE]bash
python -m simulator.producer
[PUT TRIPLE BACK TICKS HERE]

---

## Phase 6: Telemetry Extraction & Safe Teardown

**Objective:** Extract the dashboard metrics via Log Analytics and destroy the billing meters whilst protecting the Terraform state.

### 1. Telemetry Extraction
Navigate to the Log Analytics Workspace ('law-dev') in the Azure Portal. Run the following KQL query to extract the telemetry rows broadcasted by the agent, and select 'Export to CSV':

[PUT TRIPLE BACK TICKS HERE]kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-viva-dlq-agent-dev"
| where Log_s startswith "CSV_EXPORT|"
| extend csv_string = substring(Log_s, 11)
| extend columns = split(csv_string, ",")
| project 
    timestamp = columns[0],
    source_queue = columns[1],
    client_id = columns[2],
    message_type = columns[3],
    classification = columns[4],
    pattern = columns[5],
    status = columns[6],
    occurrence_count = columns[7],
    suggested_action = columns[8],
    confidence_score = columns[9]
| sort by todatetime(timestamp) desc
[PUT TRIPLE BACK TICKS HERE]

### 2. Infrastructure Teardown
From the **local laptop terminal**, execute the teardown script to destroy all compute and networking resources. This script deliberately preserves the Storage Account and Key Vault so the remote memory remains intact.

[PUT TRIPLE BACK TICKS HERE]powershell
pwsh ./infra/scripts/99-destroy-all.ps1
[PUT TRIPLE BACK TICKS HERE]

**Trap Resolution (The Bastion NSG Catch-22):** The teardown script will successfully delete the Virtual Network, but it will throw a '400 Bad Request' regarding 'nsg-azure-bastion'. This is a known Azure API race condition where Terraform attempts to delete mandatory security rules whilst Azure believes the Bastion subnet is still active.

To resolve this:
1. Go to the Azure Portal and open 'rg-viva-dlq-dev'.
2. Locate the orphaned Network Security Group ('nsg-azure-bastion') and click 'Delete' manually.
3. Run the destroy script one final time to purge the NSG from the local Terraform state cleanly:

[PUT TRIPLE BACK TICKS HERE]powershell
pwsh ./infra/scripts/99-destroy-all.ps1
[PUT TRIPLE BACK TICKS HERE]
