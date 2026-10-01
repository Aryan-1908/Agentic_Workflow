# Runbook: bad release

Sample runbook (POC knowledge base). Use case U7.
Applies to: storefront, orders-api.

## Symptoms
- Error rate or failing health checks start within an hour after a new instance template or deployment rolled out.
- Only the new version's instances fail; the old version was healthy.

## Checks
1. Find the most recent change: an instance template update, a managed instance group rolling update, a new image.
2. Compare the time of the change with the first error.

## Remediation
- Roll back to the previous instance template, action `mig.rollback`. Affects every instance of the service: needs approval.
- Do not restart instances one by one; they come back on the same bad version.

## Rollback
- `mig.rollback` can itself be undone by rolling forward to the new template once it is fixed.
