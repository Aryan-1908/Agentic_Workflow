# Runbook: TLS certificate expired

Sample runbook (POC knowledge base). Applies to HTTPS load balancers with managed certificates.
Applies to: HTTPS load balancers (none of our services).

## Symptoms
- HTTPS uptime checks fail with certificate errors; browsers show "certificate expired".

## Remediation
- Renew or re-provision the managed certificate on the load balancer.
