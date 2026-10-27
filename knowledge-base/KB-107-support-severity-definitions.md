# KB-107: Support severity definitions and response targets

**Applies to:** All OrderSync support cases | **Category:** Support policy | **Last reviewed:** October 2026

| Severity | Definition | Example | First response (Premier) | First response (Standard) |
|---|---|---|---|---|
| **Sev1** | Production down, or orders being lost, with no workaround | All order sync calls failing | 15 minutes, 24x7 | 1 hour, 24x7 |
| **Sev2** | Major production impact, with a degraded workaround | Intermittent 503s delaying orders; admins can't sign in | 1 hour | 4 business hours |
| **Sev3** | Moderate impact, non-critical feature affected | Slow reports; occasional duplicates with a workaround | 4 business hours | 1 business day |
| **Sev4** | Question or minor issue | How-to questions | 1 business day | 2 business days |

## Triage rules
- Delayed **production** orders for a Premier customer are at least **Sev2**.
- A deadline the customer mentions, such as a promotion or sale, is a reason to keep severity high and give a clear ETA. It isn't a reason by itself to raise the severity.
- Record the reasoning for the severity in an internal note.

## Follow-up rules
- Tickets in **Awaiting customer** get a polite follow-up after **48 hours** without a reply.
- After 7 days without a reply, the ticket can be resolved, with a note that it can be reopened.
