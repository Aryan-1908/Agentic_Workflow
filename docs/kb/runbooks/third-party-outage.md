# Runbook: external provider outage

Sample runbook (POC knowledge base). Use case U9.
Applies to: payments-provider, storefront.

## Symptoms
- The provider's status page shows partial or major outage for a component we use (for example card authorization).
- Our dependent flow fails at the same time (for example checkout returns 502 "payment provider failing").

## Checks
1. Confirm on the provider's status page which components are affected and since when.
2. Confirm our own services are healthy (the failure is only in calls to the provider).

## Remediation
- Nothing on our side fixes the provider: action `escalate` to on-call to decide on customer communication.
- Do not restart or roll back our services because of a provider outage.

## Rollback
- Not applicable.
