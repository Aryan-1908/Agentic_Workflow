# Runbook: sustained high CPU on a VM

Sample runbook (POC knowledge base). Use case U4 (insufficient resources).
Applies to: all services.

## Symptoms
- CPU utilization above 85% for 5 minutes or more, or a CPU anomaly well above the VM's usual level.
- Slow responses or timeouts from the service on that VM.

## Checks
1. Is it expected? Batch and report VMs run heavy scheduled jobs; check the schedule before acting.
2. Is one runaway process using the CPU (a stuck job, a loop), or is the load real traffic?
3. Is only one VM affected, or all instances of the service?

## Remediation
- Runaway process on a non-critical VM: restart the service, action `service.restart`.
- Real traffic on a managed instance group: add capacity, action `mig.resize`, bounded to at most double the current size. Needs approval.
- A single VM that is too small for its steady load: resize the machine type, action `vm.resize`. Needs approval; the VM restarts.
- Scheduled heavy job on a batch VM: no action.

## Rollback
- `mig.resize`: resize back to the previous size once load is normal.
- `vm.resize`: resize back to the previous machine type.
