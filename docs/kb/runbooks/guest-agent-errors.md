# Runbook: guest agent errors after VM start

Sample runbook (POC knowledge base). Use case U10.
Applies to: all services.

## Symptoms
- A burst of ERROR log lines from `GCEGuestAgent` shortly after a VM starts.
- Messages like "error setting initial metadatasshkey configuration: error removing unused users: failed to remove
  user <name> from google-sudoers".
- The VM itself keeps running and its services respond normally.

## Cause
- The guest agent syncs the SSH keys in instance or project metadata with local users on every boot. Users whose keys
  were removed from metadata are removed locally; when that removal fails, the agent logs one error per user.
- Harmless for the running service. It recurs on every start until the local users or the metadata are cleaned up.

## Checks
1. Confirm the VM and its services are healthy (uptime checks, application logs).
2. Compare the users named in the errors with the SSH keys in instance and project metadata.

## Remediation
- No automatic action. Report as a known, recurring, low-severity issue.
- Permanent fix (manual, by the VM owner): clean up the stale local users, or switch the VM to OS Login.
