# KB-109: How to collect and send OrderSync diagnostic logs

**Product:** OrderSync v4.x | **Audience:** Customer-facing (safe to share with customers) | **Category:** Support process | **Last reviewed:** October 2026

## When to use this article
Support needs diagnostic logs to find the cause of most problems: errors, slow syncs, orders stuck in a status, or orders missing in the warehouse. Send this article to customers when a ticket has no logs attached.

## Steps to export logs (about 5 minutes)
1. Sign in to the **OrderSync Admin portal** as an administrator.
2. Go to **Monitoring** > **Diagnostics** > **Export logs**.
3. Set the **time range** to start at least 1 hour before the problem was first noticed and end now. If you're not sure when it started, choose **Last 7 days**.
4. Under **Services**, select **All services**.
5. Leave **Include personal data** turned off. Support doesn't need it.
6. Select **Export**. A `.zip` file downloads when the export finishes.
7. Reply to your support ticket and attach the `.zip` file.

**Command line option:** run `ordersync-cli logs export --since 7d --out ordersync-logs.zip`, then attach the file.

## Also include in your reply
- **Your OrderSync version:** Admin portal > **Help** > **About**.
- **When it started:** the date and time it was first noticed (your time zone is fine).
- **Two or three examples:** order IDs (for example `SO-12345`) that show the problem.
- **Recent changes:** upgrades, configuration changes, or new integrations around that time.

## What happens next
Support reviews the logs within your plan's response target (see KB-107) and replies with findings and next steps.

## Related
KB-107 (severity definitions and response targets), KB-108 (customer communication guidelines)
