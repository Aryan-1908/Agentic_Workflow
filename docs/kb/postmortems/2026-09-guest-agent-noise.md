# Postmortem: guest agent errors escalated as an outage (September 2026)

Sample postmortem (POC knowledge base).
Applies to: all services.

## Summary
A burst of GCEGuestAgent errors after a dev VM restart was paged as an incident. The VM and its services were healthy.

## Root cause
Stale local users whose SSH keys had been removed from metadata; the guest agent could not remove them from
google-sudoers and logged one error per user on every boot.

## What fixed it
Nothing was needed for the service. The VM owner later cleaned up the stale users.

## Lessons
- Guest agent "failed to remove user" bursts right after a start are known, recurring and harmless: report, don't page.
