# KB-103: Duplicate warehouse orders caused by webhook retries

**Product:** OrderSync v4.0 and later | **Category:** Integrations | **Last reviewed:** July 2026

## Symptoms
- The warehouse occasionally receives the same pick order twice.
- It happens most often when the warehouse webhook endpoint is slow.

## Cause
OrderSync waits 10 seconds for the warehouse webhook to respond. If the endpoint finishes creating the pick order but responds after the timeout, OrderSync treats the delivery as failed and retries. The warehouse then creates the order a second time.

## Resolution
Do both of these:
1. **Use the idempotency key.** Every webhook includes an `Idempotency-Key` header. Store it and ignore any delivery whose key you've already processed.
2. **Respond quickly.** Return HTTP 200 as soon as the payload is safely queued, then create the pick order asynchronously.

## Information to request from the customer
- Does the endpoint return HTTP 200 before or after it creates the pick order?
- Does the endpoint check `Idempotency-Key`?

## Related
KB-101 (troubleshooting 503 errors, including `UpstreamWarehouseTimeout`)
