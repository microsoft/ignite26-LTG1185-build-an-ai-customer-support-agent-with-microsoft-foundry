# KB-105: API rate limits and HTTP 429 errors

**Product:** OrderSync (all versions) | **Category:** Capacity | **Last reviewed:** June 2026

## Symptoms
- Order sync calls intermittently fail with **HTTP 429 Too Many Requests** during traffic spikes, such as sales events.
- Responses include a `Retry-After` header.

## Cause
Each tenant has an API rate limit based on its support plan:

| Plan | Requests per minute | Burst |
|---|---|---|
| Standard | 1,000 | 1,500 |
| Premier | 5,000 | 7,500 |

## Resolution
1. Make sure the client honors `Retry-After` and uses exponential backoff.
2. Spread scheduled bulk syncs away from peak times.
3. For planned events, such as sales or promotions, request a temporary limit increase at least 5 business days in advance, or move to the Premier plan.

## Note
HTTP 429 means you hit a rate limit. **HTTP 503 is a different problem.** If you see 503 with `ConnectionPoolExhausted`, see KB-104 instead.

## Related
KB-101, KB-104
