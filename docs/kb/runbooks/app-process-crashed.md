# Runbook: application process crashed on a VM

Sample runbook (POC knowledge base). Use case U3.
Applies to: all services.

## Symptoms
- The VM is RUNNING but the uptime or health check for its service fails (connection refused, HTTP 502/503).
- The service's systemd unit or container shows exited / failed; error logs stop or show a crash trace.

## Checks
1. Confirm the VM itself is up (no VM stop or host error event in the audit log).
2. Look at the last error lines of the application before it stopped (out of memory, unhandled exception, bad config).
3. Check whether a deploy happened in the last hour; if so, see the bad-release runbook instead.

## Remediation
- Restart the application service: action `service.restart`. Low blast radius for one VM; can run automatically.
- If the process crashes again within 15 minutes of a restart, do not keep restarting: action `escalate`.

## Rollback
- A restart has no rollback. If the service does not report healthy within 5 minutes, escalate with the crash logs.
