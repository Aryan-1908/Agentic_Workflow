# Postmortem: storefront unreachable after firewall cleanup (June 2026)

Sample postmortem (POC knowledge base).
Applies to: storefront.

## Summary
The storefront timed out for 25 minutes. A Terraform cleanup deleted the rule allowing tcp:80 to VMs tagged storefront.

## What fixed it
Restoring the same rule (tcp:80, target tag storefront) with `firewall.restore`. A proposal to allow all ports from
0.0.0.0/0 was rejected during the incident as a security risk.

## Lessons
- Timeouts with healthy VMs: check the audit log for firewall changes first.
