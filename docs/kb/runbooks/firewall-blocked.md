# Runbook: service unreachable after a firewall change

Sample runbook (POC knowledge base). Use case U8.
Applies to: all services.

## Symptoms
- Uptime checks time out (no connection refused, just no answer) for a service whose VM is running and healthy inside.
- An audit log entry shows a firewall rule deleted or changed shortly before.

## Checks
1. Look for compute.firewalls.delete or patch in the audit log around the first failure.
2. Compare the current rules with the rule the service needs (port, source range, target tag).

## Remediation
- Restore the exact rule that was removed (same port, source range and target tag), action `firewall.restore`. Needs approval.
- Never open all ports or allow 0.0.0.0/0 on every protocol as a quick fix; that is a new security incident.

## Rollback
- Delete the restored rule if it was not the cause.
