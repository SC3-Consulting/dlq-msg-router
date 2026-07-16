# Infrastructure Deployment Runbook: Autonomous DLQ Agent

This document outlines the step-by-step procedures for provisioning the secure, Zero Trust infrastructure for the Autonomous DLQ Triage Pipeline via Terraform. It serves as a comprehensive, zero-assumption guide designed to take an operator from a blank terminal to a fully operational, AI-augmented triage agent in Azure. 

Every command, operational trap, and environmental patch required to deploy the required infrastructure and agent is documented below.

## Architectural Context & Permission Constraints

To adhere to strict enterprise security mandates, the deployment is orchestrated via either PowerShell wrappers or Bash scripts. This ensures secure injection of CLI credentials without exposing sensitive variables to version control. 

**The Resource Group Strategy:** Separating the remote state storage (Storage Account and Key Vault) into a dedicated resource group separate to the other resources is best practice. However, due to tenant-level 'User Access Administrator' restrictions preventing cross-resource-group role assignments in a restricted sandbox development environment, all infrastructure — including the state bootstrap — is encapsulated within a pre-existing resource group (e.g., `rg-dlq-msg-router-<env>`) where 'Owner' permissions are already established. This allows Terraform to autonomously execute the required 'Storage Blob Data Contributor' and 'Key Vault Secrets Officer' role assignments without elevated tenant approvals. In an Enterprise Production deployment, consideration should be made to refactor this implementation using multiple resource groups aligned with support responsibilities and appropriate boundary demarcations.

---

## Phase 0: Pre-flight Preparation & Authentication

Before initiating the deployment, authenticate the local terminal.

**Architectural Note (The Ephemeral Storage Bypass):** Azure Container Apps utilise ephemeral disks. Attempting to extract physical CSV telemetry files triggers an 'AuthorizationFailed' RBAC error. To bypass this securely, the repository's codebase (`src/run_agent.py`) is already pre-configured to broadcast telemetry directly to Azure Log Analytics via standard output. It intercepts the telemetry array and prints it using the following format: `print(f"CSV_EXPORT|{','.join(map(str, row))}")`. No manual code patching is required.

0. Base build environment

``` bash
sudo apt-get update
sudo apt-get upgrade

curl -sL https://aka.ms/InstallAzureCLIDeb | sudo bash # install Azure CLI
az extension add -n ssh

sudo apt install build-essential # optional for make commands, or just run sudo apt install make

sudo apt-get install python3-venv # create the project's Python virtual environment
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements-dev.txt # install project dependencies into the venv

sudo ./infra/scripts/install-terraform.bash # install Terraform

cp .env.example .env # create a .env file and update values as required
```

Minimum tool versions for this repository:
- Terraform >= 1.9.0
- Azure CLI >= 2.30.0 (required for `az account get-access-token --scope ...`, used by `azurerm` provider v4)

Validate the Azure CLI capability before Phase 1:

```bash
az version --query '"azure-cli"' -o tsv
az account get-access-token --scope https://graph.microsoft.com/.default -o none
```

1. Authenticate with Azure using the device code flow for secure, headless login:

```bash
az login --use-device-code
```

1a. If to be deployed to a different subscription to the default associated with your login and not selected at time of login:

```bash
az account set --subscription "My Subscription Name"
```

1b. If need to create the resource group and have permissions to do so:

```bash
# check if resource group exists
TARGET_ENV="dev" # or test or prod
TARGET_LOCATION="australiaeast"
az group show --name "rg-dlq-msg-router-${TARGET_ENV}"

# if does not exist, create (assuming you have permissions to do so within the subscription)
az group create --name "rg-dlq-msg-router-${TARGET_ENV}" --location "${TARGET_LOCATION}"
```

2. **Trap Resolution (Soft-Deleted Vaults):** Azure retains Key Vaults in a soft-deleted state, blocking recreation. If a previous deployment was torn down, purge the old vault manually before proceeding. Replace the name with your specific vault target:

```bash
az keyvault purge --name <kvtfstatexxxxxx> --location australiaeast
```

---

## Phase 1: Remote State Bootstrap

**Objective:** Provision the foundational Azure resources required to securely execute Terraform. This creates the Storage Account for the '.tfstate' file and the Key Vault, and handles all SSH key generation securely.

1. Execute the Phase 1 Bash orchestrator from your local repository root:

```bash
TARGET_ENV="dev" # or test or prod
TARGET_LOCATION="australiaeast"
bash ./infra/scripts/01-bootstrap.sh -e "${TARGET_ENV}" -l "${TARGET_LOCATION}"
```

2. **Automated State & Security Initialisation:** The `01-bootstrap.sh` script will autonomously generate the local SSH key pair (if not present), provision the remote state storage, inject the public key into the Key Vault, and generate the `backend.hcl` configuration. 

**Architectural Note (Zero Trust Key Management):** The private SSH key is intentionally *not* stored in the Key Vault. Storing a private key in a centralised vault creates an escalation vulnerability. To maintain least-privilege Zero Trust, the private key remains strictly on the operator's local machine (or ephemeral CI/CD runner), whilst only the public key is distributed to the vault and target infrastructure.

*(Note: Terraform initialisation for the main environment is handled automatically by the subsequent wrapper scripts. No manual `terraform init` is required).*

---

## Phase 2: Network & Data Plane Deployment

**Objective:** Deploy the Virtual Network, Private Endpoints, Azure Container Registry (ACR), Premium Service Bus, and the Azure Foundry cognitive accounts.

1. Execute the Phase 2 orchestrator to deploy the VNet, ACR, Service Bus, and Azure Foundry endpoints:

```bash
bash ./infra/scripts/02-deploy-network-and-data.sh -e "${TARGET_ENV}"
```

2. Commit the structural changes to version control to ensure the remote repository is prepared for the jumpbox pull:

```bash
git add .
git commit -m "Phase 2: IaC modules, runbooks, and strict repo cleanup"
git push
```

---

## Phase 3: Jumpbox Configuration & Image Push

**Objective:** Securely automate the jumpbox configuration via an Azure Bastion Native Client tunnel, inject the dynamic environment variables, and push the Docker image.

### 1. Automate Jumpbox Provisioning
The Jumpbox is a naked Ubuntu instance. We utilise a Bash script to establish a secure Bastion SSH tunnel. The script automatically installs `docker.io`, `python3-venv`, and `terraform`, clones the repository, and dynamically generates the simulator `.env` file using Terraform state outputs.

TODO: Fix numeric ordering of scripts to reflect order they are to be run in.

export TARGET_BRANCH="main" # Change this if testing on a different branch
bash ./infra/scripts/03-configure-jumpbox.sh -e "${TARGET_ENV}"

Execute this from your **local laptop terminal**:

```bash
bash ./infra/scripts/03-configure-jumpbox.sh -e "${TARGET_ENV}"
```
### 2. Connect via Bastion & Push the Image
Once the provisioning script completes successfully, use the Azure CLI to tunnel directly into the Jumpbox. 

```bash
TARGET_ENV="dev"
az network bastion ssh --name "bas-${TARGET_ENV}" --resource-group "rg-dlq-msg-router-${TARGET_ENV}" --target-resource-id $(az vm show --resource-group "rg-dlq-msg-router-${TARGET_ENV}" --name "vm-jumpbox-${TARGET_ENV}" --query id -o tsv) --auth-type "ssh-key" --username "azureuser" --ssh-key ~/.ssh/dlq_jumpbox_rsa
```
Fallback manual workflow if script fails to run:
1. Navigate to the Azure Portal.
2. Open `vm-jumpbox-<env>` and select 'Connect' -> 'Bastion'.
3. Username: 'azureuser'
4. Authentication Type: 'SSH Private Key from Local File'.
5. Upload the private key (`~/.ssh/dlq_jumpbox_rsa` - select the file WITHOUT the `.pub` extension).
6. Uncheck 'Open in new browser tab', then click 'Connect'.

Then while still on the jumpbox, execute the image push. *(Note: The Jumpbox Managed Identity has been granted the `Storage Blob Data Contributor` role, allowing it to seamlessly read the remote state and retrieve the ACR credentials).*

**RBAC Propagation Note:** New or recently changed role assignments can take a short time to become effective. If `04-push-image.bash` fails during Terraform backend initialisation with a `403 AuthorizationPermissionMismatch`, wait 1-2 minutes and retry the command.

```bash
cd dlq-msg-router
TARGET_ENV="dev"
bash ./infra/scripts/04-push-image.bash -e "${TARGET_ENV}"
```

Once the image push completes, type `exit` to return to your local terminal.

---

## Phase 4: Agent Hosting Deployment & Code Patching

**Objective:** Deploy the agent to Azure Container Apps and bypass ephemeral storage restrictions.

### 1. Trap Resolution: The Identity Crash
When mapping User-Assigned Managed Identities into Container Apps, the Python 'DefaultAzureCredential' defaults to System-Assigned, causing a fatal 'ClientAuthenticationError'. The Terraform configuration automatically resolves this by querying the 'client_id' and injecting it as 'AZURE_CLIENT_ID'.

*(Architectural Note: User-Assigned identity is deliberately utilised over System-Assigned to decouple RBAC assignments from the container lifecycle. This allows Terraform to establish secure permissions before the container is provisioned, preventing Infrastructure-as-Code race conditions).*

### 2. Trap Resolution: Immutable Tag Rollout
Terraform deploys the image tag declared in `infra/terraform/azure/environments/<env>/platform.tfvars` (`container_image_tag`).

To roll out a new image revision, the pipeline must first build and push the updated image to the Azure Container Registry. Simply updating the variable in Terraform will not trigger a Docker build. Once the image is successfully pushed via the Phase 3 bash scripts, executing the Phase 4 deployment script will force Terraform to detect the tag change and deploy the new revision to the Container App.

### 3. Execute Deployment
Return to the **local laptop terminal** and deploy the agent. 

**Architectural Note (Dynamic Variables):** The deployment module natively extracts `SERVICE_BUS_FULLY_QUALIFIED_NAMESPACE` and `AZURE_FOUNDRY_ENDPOINT` directly from the Terraform outputs. These are dynamically injected into the Azure Container App environment variables, preventing startup crashes.

```bash
bash ./infra/scripts/05-deploy-agent.sh -e "${TARGET_ENV}"
```

## Phase 5: Live Fire Simulation

**Objective:** Inject synthetic enterprise traffic to validate schema auto-healing, duplicate dropping, and Azure Foundry fallback.

**Trap Resolution (Module Execution):** Running Python scripts directly via path triggers a 'ModuleNotFoundError' due to execution isolation. They must be executed as modules from the repository root.


1. Open **two separate Bastion terminals** connected to the Jumpbox.
2. In BOTH terminals, activate the virtual environment:

```
cd ~/dlq-msg-router
source .venv/bin/activate
```

3. **Terminal 1 (Sanitise the Environment):** Flush all ghost messages from the Service Bus:

```bash
python -m src.flush_queues
```

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
| extend columns = parse_csv(csv_string)
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
From the **local laptop terminal**, execute the teardown script to destroy all compute and networking resources. This standard execution deliberately preserves the Storage Account and Key Vault so the Terraform state remains intact.

```bash
bash ./infra/scripts/99-destroy-all.sh -e "${TARGET_ENV}"
```

*(Architectural Note: If a complete eradication of the environment is required, including the underlying Terraform state files, Storage Account, and Key Vault, append the `--full-purge` flag to the command above).*

**Trap Resolution (The Bastion NSG Catch-22):** The teardown script will successfully delete the Virtual Network, but it may throw a '400 Bad Request' regarding 'nsg-azure-bastion'. This is a known Azure API race condition where Terraform attempts to delete mandatory security rules whilst Azure believes the Bastion subnet is still active.

To resolve this:
1. Go to the Azure Portal and open `rg-dlq-msg-router-<env>`.
2. Locate the orphaned Network Security Group (`nsg-azure-bastion`) and click 'Delete' manually.
3. Run the destroy script one final time to purge the NSG from the local Terraform state cleanly:

```bash
bash ./infra/scripts/99-destroy-all.sh -e "${TARGET_ENV}"
```
