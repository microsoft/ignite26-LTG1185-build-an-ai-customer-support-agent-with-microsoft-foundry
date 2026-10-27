# KB-104: Intermittent HTTP 503 errors (ConnectionPoolExhausted) after upgrading to OrderSync v4.2

**Product:** OrderSync v4.2.0 | **Fixed in:** v4.2.1 | **Category:** Availability, upgrades | **Last reviewed:** October 2026

## Symptoms
After upgrading from v4.1.x to v4.2.0:
- Some order sync calls (often 10 to 25%) fail with **HTTP 503**, mostly at peak traffic times such as lunchtime.
- Retries usually succeed, so orders arrive late rather than being lost.
- Logs show:
  `HTTP 503 ServiceUnavailable: ConnectionPoolExhausted (pool_size=20, in_use=20, ...)`
- Before the failures start, you may see: `Connection pool near capacity (pool_size=20, ...)`.

## Cause
OrderSync v4.2.0 changed the **default database connection pool size from 100 to 20** to reduce idle connections for small tenants. Tenants with high order volume run out of connections at peak load. New requests then wait, time out, and return 503.

## Resolution
Choose one:

**Option A: Quick fix, no downtime (recommended right away)**
1. Set the environment variable `ORDERSYNC_POOL_SIZE=100`. Use a higher value for tenants processing more than 50,000 orders per hour.
2. Restart the OrderSync API service (a rolling restart is fine).
3. Check that the logs now show `pool_size=100` and that the 503s stop at the next peak.

**Option B: Permanent fix**
Upgrade to **OrderSync v4.2.1**, which restores the default pool size to 100.

## How to confirm the diagnosis
- The 503s started right after the v4.2.0 deployment (look for `Deployment completed: ... -> 4.2.0`).
- The 503 log lines include `ConnectionPoolExhausted` and `pool_size=20`.
- The error rate rises and falls with traffic.

## Severity guidance
**Sev2** for production tenants with delayed orders. Raise it to **Sev1** if orders are being lost or all requests fail. See KB-107.

## Related
KB-101 (troubleshooting 503 errors), KB-105 (rate limits, which can look similar but return 429)
