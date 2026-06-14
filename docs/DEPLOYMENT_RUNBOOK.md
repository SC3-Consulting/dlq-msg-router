# Infrastructure Deployment Runbook: Autonomous DLQ Agent

This document outlines the step-by-step procedures for provisioning the secure, Zero Trust infrastructure for the Autonomous DLQ Triage Pipeline via Terraform.

## Architectural Overview

To adhere to strict enterprise security and cost-control mandates, the deployment is orchestrated via PowerShell wrappers. This approach ensures secure injection of CLI credentials without exposing sensitive variables to version control, while guaranteeing asynchronous Azure resource providers are fully initialised prior to infrastructure deployment.

The deployment is split into distinct phases to avoid Terraform circular dependencies and to safely isolate the remote state memory from the ephemeral compute resources.

---

## Phase 1: Remote State Bootstrap

**Objective:** Provision the foundational Azure resources required to securely execute Terraform. This includes an Azure Storage Account to hold the `.tfstate` file and an Azure Key Vault to securely store the Jumpbox SSH keys.

**Strategic Note (Role-Based Access Control):** To bypass tenant-level `User Access Administrator` restrictions and prevent sprint blockers, the state storage is explicitly encapsulated within the pre-existing `rg-viva-dlq-dev` resource group where the operator already possesses 'Owner' permissions. This allows Terraform to autonomously execute the required `Storage Blob Data Contributor` and `Key Vault Secrets Officer` role assignments.

### 1. Pre-flight Requirements

Ensure your local terminal (or WSL environment) is authenticated with Azure and has the necessary permissions.

    az login

Ensure your repository's `.gitignore` has been updated to prevent the accidental commit of the generated backend topologies:

    # Terraform generated backend configurations
    infra/terraform/azure/environments/**/backend.hcl
    infra/terraform/azure/environments/**/bootstrap.generated.tfvars

### 2. Execution

Navigate to the root of the repository and execute the Phase 1 PowerShell orchestrator.

    pwsh ./infra/scripts/01-bootstrap.ps1

### 3. Validation and Expected Outputs

The script will automatically register required Azure API providers (such as `Microsoft.ContainerRegistry` and `Microsoft.ServiceBus`), pausing execution until the Azure Management Plane confirms the registrations are finalised.

Upon successful application of the Terraform graph, the script will output a success block containing your generated, randomised resource names:

    ------------------------------------------------------
    Phase 1 Bootstrap Completed Successfully.
    Storage Account : sttfstatedev[random_suffix]
    Key Vault       : kvtfstate[random_suffix]
    ------------------------------------------------------

The orchestrator will automatically write these values into `infra/terraform/azure/environments/dev/backend.hcl`, officially bringing your Terraform remote memory bank online.