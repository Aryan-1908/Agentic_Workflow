# Runbook: application cannot connect to the database

Sample runbook (POC knowledge base). Use case U6.
Applies to: orders-db, orders-api, storefront, reports-batch.

## Symptoms
- An API's health check fails with "database unreachable" and its dependents (for example the storefront checkout) return 502.
- Alerts arrive from several services at once: the database, the API that uses it, and the storefront.

## Cause
- Usually the database side: the database VM stopped, PostgreSQL is down, or the network path to port 5432 is blocked.
- Fixing the API or the storefront does not help while the database is down.

## Checks
1. Is the database VM running? Look for a VM stop or host error event on the database VM.
2. Is PostgreSQL listening on 5432? Is there a firewall change on the internal rule?

## Remediation
- Database VM stopped: start it, action `vm.start`. Tier 1 data service: needs approval.
- Firewall rule for port 5432 removed: restore it, action `firewall.restore`, limited to the internal range. Needs approval.
- Never restart the API or the storefront as the fix; they recover once the database is back.

## Rollback
- `vm.start`: none. `firewall.restore`: remove the restored rule if it causes a new problem.
