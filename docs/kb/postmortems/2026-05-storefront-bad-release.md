# Postmortem: storefront release 2026.05.2 (May 2026)

Sample postmortem (POC knowledge base).
Applies to: storefront.

## Summary
Checkout error rate rose to 30% within 10 minutes of rolling out instance template storefront-2026-05-2.

## Root cause
The new template referenced a missing configuration key for the payment client.

## What fixed it
Rolling the managed instance group back to the previous template (`mig.rollback`), approved by the on-call engineer.
Restarting individual instances earlier had no effect.

## Lessons
- Errors that start right after a template rollout mean rollback first, investigate second.
