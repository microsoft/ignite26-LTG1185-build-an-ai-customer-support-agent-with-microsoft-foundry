# KB-106: Daily order report is slow or times out (missing index)

**Product:** OrderSync Reporting, v4.1 and later | **Category:** Performance | **Last reviewed:** September 2026

## Symptoms
- The daily order summary report, which used to take about 2 minutes, now takes 20 minutes or more, or times out.
- Logs show `Report dataset row count exceeded 5,000,000 - query plan changed to full scan`, or `Slow query: daily_order_summary ... (missing index on orders.created_date)`.

## Cause
When a tenant's order table grows past about 5 million rows, the report query switches to a full table scan unless an index exists on `orders.created_date`. Tenants created before v4.1 don't have this index automatically.

## Resolution
1. In the OrderSync admin portal, go to **Reporting > Maintenance** and select **Create recommended indexes**. The job runs online and takes 5 to 15 minutes.
2. Rerun the report. Expect it to finish in about 2 minutes.
3. Optionally, set data retention to archive orders older than 18 months.

## Severity guidance
Usually **Sev3**: reporting is affected, but order processing isn't. See KB-107.

## Related
KB-107
