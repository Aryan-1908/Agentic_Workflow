# Runbook: disk filling up

Sample runbook (POC knowledge base). Use case U5.
Applies to: all services.

## Symptoms
- Disk usage above 90% on a VM, write errors ("No space left on device") in application logs.

## Checks
1. Which disk: the boot disk (logs, temp files) or a data disk (database files)?
2. What is growing: application logs, journal, temp files, or data?

## Remediation
- Boot disk filled by logs on a stateless VM: rotate and compress old logs, action `logs.rotate`. Low risk; can run automatically.
- Data disk of a database (for example orders-db): never delete or rotate files. Action `escalate` to the database owner; data loss risk.
- Growth that returns within a day after rotation: escalate so retention can be fixed.

## Rollback
- `logs.rotate` keeps compressed archives for 7 days; nothing to undo.
