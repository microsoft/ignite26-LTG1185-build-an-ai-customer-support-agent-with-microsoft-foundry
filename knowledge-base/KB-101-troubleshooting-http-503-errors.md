# KB-101: Troubleshooting HTTP 503 errors in OrderSync

**Product:** OrderSync (all versions) | **Category:** Availability | **Last reviewed:** September 2026

## Summary
HTTP 503 (Service Unavailable) means OrderSync could not process the request at that moment. Most 503s are temporary, but repeated or patterned 503s usually have a specific cause. Use this article to find which one applies, then follow the linked article.

## Step 1: Check the error detail
Every 503 response and log line includes a reason code. Match it below.

| Reason code in logs | Likely cause | Go to |
|---|---|---|
| `ConnectionPoolExhausted` | Database connection pool too small for the load | **KB-104** |
| `MaintenanceInProgress` | Scheduled platform maintenance | Status page |
| `UpstreamWarehouseTimeout` | The customer's warehouse endpoint is slow | KB-103 |
| `RateLimited` (returned as 429, not 503) | API rate limit exceeded | KB-105 |

## Step 2: Look for a pattern
- **Started right after an upgrade?** Compare configuration defaults between versions. See the release notes and KB-104.
- **Only at peak times?** Points to a capacity limit (connection pool or rate limit), not an outage.
- **Every request fails?** Check the status page. This is likely a platform incident, so raise a Sev1.

## Step 3: Collect before escalating
- Ticket ID, OrderSync version, and environment
- 10 to 20 log lines around the first failure, including the reason code
- Time of the last change (upgrade, configuration, or traffic event)

## Related
KB-104 (connection pool exhaustion after v4.2), KB-105 (rate limits), KB-107 (severity definitions)
