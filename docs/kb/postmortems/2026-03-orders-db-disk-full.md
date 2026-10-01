# Postmortem: orders-db data disk full (March 2026)

Sample postmortem (POC knowledge base).
Applies to: orders-db, orders-api, storefront.

## Summary
Orders could not be placed for 40 minutes. The orders-db data disk reached 100%; PostgreSQL stopped accepting writes.
orders-api and the storefront checkout alerted at the same time; the first responder restarted orders-api, which did
not help.

## Root cause
WAL archive files accumulated on the data disk after the archive job's destination bucket was deleted.

## What fixed it
The database owner restored the archive destination and cleaned archived WAL files. Rotating or deleting files on
the data disk without the owner would have risked data loss.

## Lessons
- Alerts from orders-db, orders-api and storefront together point at the database first.
- Data disk problems on orders-db are escalated to the database owner, never auto-remediated.
