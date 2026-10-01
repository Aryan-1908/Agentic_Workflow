# Postmortem: runaway report job (August 2026)

Sample postmortem (POC knowledge base).
Applies to: reports-batch.

## Summary
batch-01 ran at 100% CPU for 6 hours; a report job was stuck in a retry loop. No customer impact.

## What fixed it
Restarting the report service (`service.restart`). The nightly scheduled job, which also uses high CPU for about
30 minutes, is expected and needs no action.

## Lessons
- High CPU on a batch VM: distinguish the scheduled window from a stuck job before acting.
