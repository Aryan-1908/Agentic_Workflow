# Runbook: Cloud SQL failover

Sample runbook (POC knowledge base). Applies only to Cloud SQL instances; orders-db is self-managed PostgreSQL on a VM.
Applies to: Cloud SQL instances (none of our services).

## Symptoms
- Cloud SQL instance reports a failover to its standby; connections drop for up to a minute.

## Remediation
- Wait for the failover to finish; clients reconnect. Cloud SQL only.
