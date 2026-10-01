# Postmortem: PayFast authorization outage (July 2026)

Sample postmortem (POC knowledge base).
Applies to: payments-provider, storefront.

## Summary
Card payments failed for 55 minutes. PayFast's status page showed a partial outage of the Card Authorization API.

## What happened
Our on-call rolled back the storefront, suspecting our release. It made no difference: the cause was the provider.

## Lessons
- Check the provider status page before touching our own services when checkout fails on payment calls.
- Provider outages are escalated for customer communication; there is nothing to remediate on our side.
