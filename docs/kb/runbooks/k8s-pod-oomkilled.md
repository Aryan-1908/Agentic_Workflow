# Runbook: Kubernetes pod OOMKilled

Sample runbook (POC knowledge base). Applies only to GKE workloads; Acme Shop runs on VMs today.
Applies to: GKE workloads (none of our services).

## Symptoms
- Pods restart with reason OOMKilled; container memory hits its limit.

## Remediation
- Raise the container memory limit in the deployment, or fix the memory leak. Kubernetes workloads only.
