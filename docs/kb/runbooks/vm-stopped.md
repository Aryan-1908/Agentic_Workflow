# Runbook: VM stopped unexpectedly

Sample runbook (POC knowledge base). Use case U1.
Applies to: all services.

## Symptoms
- A VM audit event "VM stopped" by a user or a service account, outside its instance schedule.
- Uptime checks or health checks for the service on that VM start failing.
- Dependent services report connection errors.

## Checks
1. Look at the audit log entry for the stop: who stopped it (principal), and when.
2. If the principal is the instance schedule (compute-system service account), the stop is expected. No action.
3. Check whether a deployment or maintenance window was announced for that time.
4. Check the VM's serial console and system events for a guest shutdown or host error before the stop.

## Remediation
- Expected stop (schedule or announced maintenance): no action; record as context.
- Unexpected stop of a tier 3 service: start the VM (`vm.start`); low blast radius, can run automatically.
- Unexpected stop of a tier 1 or tier 2 service: start the VM (`vm.start`) after approval.
- Repeated stops by the guest OS or host errors: escalate to on-call; do not keep restarting.

## Rollback
- `vm.start` has no rollback beyond stopping the VM again. If the service fails to come up healthy within
  10 minutes, escalate.
