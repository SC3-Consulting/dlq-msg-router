# Infrastructure Deployment Runbook: Autonomous DLQ Agent

This document outlines the step-by-step procedures for provisioning the secure, Zero Trust infrastructure for the Autonomous DLQ Triage Pipeline via Terraform. It serves as a comprehensive, zero-assumption guide designed to take an operator from a blank terminal to a fully operational, AI-augmented triage agent in Azure. 

Every command, operational trap, and environmental patch required to deploy the required infrastructure and agent is documented below.

## Architectural Context & Permission Constraints

To adhere to strict enterprise security mandates, the deployment is orchestrated via PowerShell wrappers. This ensures secure injection of CLI credentials without exposing sensitive variables to version control. 

**The Resource Group Strategy:** Separating the remote state storage (Storage Account and Key Vault) into a dedicated resource group separate to the other resources is best practice. However, due to tenant-level 'User Access Administrator' restrictions preventing cross-resource-group role assignments in a restricted sandbox development environment, all infrastructure — including the state bootstrap — is encapsulated within a pre-existing resource group (e.g., `rg-dlq-msg-router-<env>`) where 'Owner' permissions are already established. This allows Terraform to autonomously execute the required 'Storage Blob Data Contributor' and 'Key Vault Secrets Officer' role assignments without elevated tenant approvals. In an Enterprise Production deployment, consideration should be made to refactor this implementation using multiple resource groups aligned with support responsibilities and appropriate boundary demarcations.

---

## Phase 0: Pre-flight Preparation & Authentication

Before initiating the deployment, authenticate the local terminal and prepare the secure SSH keys required for jumpbox access.

**Architectural Note (The Ephemeral Storage Bypass):** Azure Container Apps utilise ephemeral disks. Attempting to extract physical CSV telemetry files triggers an 'AuthorizationFailed' RBAC error. To bypass this securely, the repository's codebase (`src/run_agent.py`) is already pre-configured to broadcast telemetry directly to Azure Log Analytics via standard output. It intercepts the telemetry array and prints it using the following format: `print(f"CSV_EXPORT|{','.join(map(str, row))}")`. No manual code patching is required.

1. Authenticate with Azure using the device code flow for secure, headless login:

```bash
az login --use-device-code
```

2. Generate a local SSH key pair to securely access the Azure jumpbox VM from a Bastion Host. Do not set a passphrase when prompted:

```bash
ssh-keygen -m PEM -t rsa -b 4096 -f ~/.ssh/dlq_jumpbox_rsa -N ""
```
TODO: CI/CD autocreates the SSH key and stores it in the Key Vault from which the Bastion Host accesses it for remote access

3. **Trap Resolution (Soft-Deleted Vaults):** Azure retains Key Vaults in a soft-deleted state, blocking recreation. If a previous deployment was torn down, purge the old vault manually before proceeding. Replace the name with your specific vault target:

```bash
az keyvault purge --name kvtfstatelogia7 --location australiaeast
```

---

## Phase 1: Remote State Bootstrap

**Objective:** Provision the foundational Azure resources required to securely execute Terraform. This creates the Storage Account for the '.tfstate' file and the Key Vault.

1. Execute the Phase 1 PowerShell orchestrator from your local repository root:

```powershell
pwsh ./infra/scripts/01-bootstrap.ps1
```
TODO: Create a Linux shell (eg Bash) equivalent. Not all Linux environments run, or want to install PowerShell. Using PowerShell makes many assumptions about the environment this is being executed from. As instructions here include Bash commands and there is at least one Bash script, it can be assumed that it is expected to perform these steps from a Linux environment and almost all Linux environments can run Bash. Perhaps call this out at the top of the instructions.

2. **Secure Key Injection:** Once the Key Vault is provisioned, manually read the generated public key and inject it into the vault as a secret. Execute these commands in your local PowerShell terminal - changing the resource names as appropriate:

```powershell
$PubKey = Get-Content ~/.ssh/dlq_jumpbox_rsa.pub -Raw
az keyvault secret set --vault-name kvtfstatelogia7 --name jumpbox-admin-ssh-public-key-<env> --value "$PubKey"
```
TODO: Automate this as part of the provisioning process, and be consistent in the scripting choices. Is there a reason for not also storing the private key in Key Vault?

3. Initialise the Terraform backend memory to link the local workspace to the newly created Storage Account:

```bash
cd infra/terraform/azure
terraform init -reconfigure -backend-config=environments/<env>/backend.hcl
cd ../../..
```
TODO: Automate this as part of the provisioning process

---

## Phase 2: Network & Data Plane Deployment

**Objective:** Deploy the Virtual Network, Private Endpoints, Azure Container Registry (ACR), Premium Service Bus, and the Azure Foundry cognitive accounts.

1. Execute the Phase 2 orchestrator:

```powershell
pwsh ./infra/scripts/02-deploy-network-and-data.ps1
```
TODO: Create a Linux shell (eg Bash) equivalent

2. Commit the structural changes to version control to ensure the remote repository is prepared for the jumpbox pull:

```bash
git add .
git commit -m "Phase 2: IaC modules, runbooks, and strict repo cleanup"
git push
```

---

## Phase 3: Jumpbox Configuration & The Split-Brain Fix

**Objective:** Securely access the jumpbox VM via an Azure Bastion instance, configure the jumpbox, inject the environment variables, and push the Docker image.

### 1. Access the Jumpbox
1. Navigate to the Azure Portal.
2. Open `vm-jumpbox-<env>` and select 'Connect' -> 'Bastion'.
3. Username: 'azureuser'
4. Authentication Type: 'SSH Private Key from Local File'.
5. Upload the private key (`~/.ssh/dlq_jumpbox_rsa` - select the file WITHOUT the `.pub` extension).
6. Uncheck 'Open in new browser tab', then click 'Connect'.

TODO: Azure Bastion should be able to pull the private SSH key from Key Vault

### 2. Configure the Jumpbop Environment
The Jumpbox is a naked Ubuntu instance. Paste the following block directly into the jumpbox terminal to install dependencies, clone the code, and bypass PEP 668 restrictions. Replace 'YOUR_GITHUB_USERNAME' with the actual username.

```bash
curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash
sudo apt-get update
sudo apt-get install -y docker.io python3-pip python3-venv
sudo chmod 666 /var/run/docker.sock
git clone https://github.com/YOUR_GITHUB_USERNAME/message-router-agent.git
cd message-router-agent
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```
TODO: Consider scripting this

TODO: Having a github path different to the original implies that anyone following these instructions have taken a fork of this code. That is not called out anywhere.

### 3. The Split-Brain Environment Patch (Jumpbox Local Simulation Only)
**Trap Resolution:** The cloud agent dynamically discovers queues, whilst the Jumpbox simulators default to reading local JSON files. If a queue name has a typo in the local JSON, the simulators will crash with an 'amqp:not-found' error. 

TODO: Make the simulators less brittle so that they don't crash on a queue mismatch

To synchronise the Jumpbox with the production logic, the simulators must be forced to hit the Service Bus API. Paste this exact configuration into the Jumpbox terminal to create a local simulation `.env` file.

TODO: Script this with values auto-pulled from Terraform. On a git clone/pull on the jumpbox, the script can be downloaded and run on the jumpbox rather than copy/paste with values that could be stale from a Markdown page, with instructions here on what might need configuration in the created .env file post creation.

Note: This local file is only for simulator execution on the jumpbox. Azure Container Apps deployment uses `environments/<env>/platform.tfvars` and Key Vault-backed secret mappings, not a copied developer `.env` file.

```bash
cat << 'EOF' > .env
# Azure Service Bus Configuration
SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE="sb-msg-router-.servicebus.windows.net"

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
# GPU recommendation (subject to VRAM):
# - >= 8 GB VRAM:  qwen2.5:7b-instruct or llama3.1:8b-instruct-q4_K_M
# - >= 16 GB VRAM: qwen2.5:14b-instruct-q4_K_M

# File Paths
RULES_FILE_PATH="data/rules.json"


# Azure Foundry Specifics (Required if AI_PROVIDER="AZURE_FOUNDRY")
AI_PROVIDER="AZURE_FOUNDRY"
AZURE_FOUNDRY_ENDPOINT="https://foundry-msg-router.cognitiveservices.azure.com/openai/deployments/gpt-4o-mini"
AZURE_FOUNDRY_DEPLOYMENT_NAME="gpt-4o-mini"
AZURE_FOUNDRY_TEMPERATURE=0.1
# Time to sleep (in seconds) after completing a full sweep of all queues
# Prevents aggressive AMQP link churn if all DLQs are empty.
AGENT_CYCLE_SLEEP_SECONDS=60
# Dynamic Cost Controls (Your Real-Time Airbags)
AZURE_FOUNDRY_MAX_TOKENS=300
EOF
```

### 4. Build and Push the Image
Execute the bash script to compile and push the container to the ACR using the Jumpbox's Managed Identity:

```bash
bash ./infra/scripts/03-push-image.bash
```
TODO: Include instructions on managing the container image's immutable tag

---

## Phase 4: Agent Hosting Deployment & Code Patching

**Objective:** Deploy the agent to Azure Container Apps and bypass ephemeral storage restrictions.

### 1. Trap Resolution: The Identity Crash
When mapping User-Assigned Managed Identities into Container Apps, the Python 'DefaultAzureCredential' defaults to System-Assigned, causing a fatal 'ClientAuthenticationError'. The Terraform configuration automatically resolves this by querying the 'client_id' and injecting it as 'AZURE_CLIENT_ID'.

TODO: Assess whether a System-Assigned Managed Identity would be a better implementation.

### 2. Trap Resolution: Immutable Tag Rollout
Terraform deploys the image tag declared in `infra/terraform/azure/environments/<env>/platform.tfvars` (`container_image_tag`).

To roll out a new image revision, build a new image with updated image tag and update `container_image_tag` in the target environment `tfvars` file and re-run deployment as per the next step.

### 3. Execute Deployment
Return to the **local laptop terminal** and deploy the agent. Terraform will detect the altered `AGENT_CYCLE_SLEEP_SECONDS` variable and force a fresh container deployment.

TODO: A changed variable will be identified by Terraform when running ./infra/scripts/03-push-image.bash and trigger a rebuild and redeploy to ACR. That of itself will not cause ./infra/scripts/04-deploy-agent.ps1 to redeploy to ACA if the image has not first been updated.

```powershell
pwsh ./infra/scripts/04-deploy-agent.ps1
```
TODO: Create a Linux Shell (eg Bash) equivalent

TODO: Just changing the immutable tag should be a sufficient touch for Terraform to identify a new build / deployment to ACR and from ACR to ACA is required without having to touch any variable values that change functional behaviour. But while any corresponding change will cause Terraform to rebuild/redeploy, the change itself doesn't trigger anything. You still have to run the above scripts that trigger Terraform to do the builds/deploys.

---

## Phase 5: Live Fire Simulation

**Objective:** Inject synthetic enterprise traffic to validate schema auto-healing, duplicate dropping, and Azure Foundry fallback.

**Trap Resolution (Module Execution):** Running Python scripts directly via path triggers a 'ModuleNotFoundError' due to execution isolation. They must be executed as modules from the repository root.

TODO: Make modules more robust from where they can be run from

1. Open **two separate Bastion terminals** connected to the Jumpbox.
2. In BOTH terminals, activate the virtual environment:

```bash
cd ~/message-router-agent
source .venv/bin/activate
```

3. **Terminal 1 (Sanitise the Environment):** Flush all ghost messages from the Service Bus:

```bash
python -m src.flush_queues
````

4. **Terminal 1 (The Consumer):** Start the downstream application simulator:

```bash
python -m simulator.consumer
```

5. **Terminal 2 (The Payload Cannon):** Dispatch the synthetic anomalies:

```bash
python -m simulator.producer
```

---

## Phase 6: Telemetry Extraction & Safe Teardown

**Objective:** Extract the dashboard metrics via Log Analytics and destroy the billing meters whilst protecting the Terraform state.

### 1. Telemetry Extraction
Navigate to the Log Analytics Workspace (`law-<env>`) in the Azure Portal. Run the following KQL query to extract the telemetry rows broadcasted by the agent, and select 'Export to CSV':

```kusto
ContainerAppConsoleLogs_CL
| where ContainerAppName_s == "ca-dlq-msg-router-<env>"
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
```
For additional LAW KQL queries, see [OPERATOR_DASHBOARD_QUERIES](OPERATOR_DASHBOARD_QUERIES.md)

### 2. Infrastructure Teardown
From the **local laptop terminal**, execute the teardown script to destroy all compute and networking resources. This script deliberately preserves the Storage Account and Key Vault so the Terraform state remains intact.

TODO: add an option to the script to also include tear down of the Storage Account and Key Vault used by Terraform.

```powershell
pwsh ./infra/scripts/99-destroy-all.ps1
```
TODO: Create a Linux shell (eg Bash) script equivalent

**Trap Resolution (The Bastion NSG Catch-22):** The teardown script will successfully delete the Virtual Network, but it will throw a '400 Bad Request' regarding 'nsg-azure-bastion'. This is a known Azure API race condition where Terraform attempts to delete mandatory security rules whilst Azure believes the Bastion subnet is still active.

To resolve this:
1. Go to the Azure Portal and open `rg-dlq-msg-router-<env>`.
2. Locate the orphaned Network Security Group (`nsg-azure-bastion`) and click 'Delete' manually.
3. Run the destroy script one final time to purge the NSG from the local Terraform state cleanly:

```powershell
pwsh ./infra/scripts/99-destroy-all.ps1
```
