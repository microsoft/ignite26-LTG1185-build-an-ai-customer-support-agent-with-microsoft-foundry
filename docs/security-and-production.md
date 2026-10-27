# Security and production readiness

This sample is built to be safe to deploy and easy to learn from. Some shortcuts that make a demo smooth are not what you'd ship. This page lists what's already done, what to change for production, and why.

## What's already in place

- **No keys in code or config.** Every Azure service authenticates with Entra ID and managed identities. Local (key) auth is disabled on the Foundry account, and shared-key access is disabled on Storage. The ticket-system MCP key is stored only in a Foundry project connection.
- **Least-privilege tools.** Each prompt agent can call only the ticket-system tools it needs (`allowed_tools`). No prompt agent can send a customer email.
- **A human approves every customer message.** The approval gate is a step in the Case Manager's workflow code, not an instruction in a prompt. Sending happens in one code path that only runs on an approve decision.
- **Guardrails on every agent**, including prompt-injection detection on tool results (the ticket text and logs that come from outside your company). See [architecture: guardrails](architecture.md#guardrails).
- **Policy enforced in code.** Response-time promises are computed from the support policy, internal KB references are removed from customer emails, and drafts that mention internal articles are logged.
- **Admin operations aren't tools.** Demo reset is an HTTP endpoint protected by a function key and isn't exposed over MCP, so no agent can call it.
- **Adversarial evals.** The drafting eval set includes tickets with hidden instructions to leak internal notes, other customers' data, or a malicious link.

## Change these for production

### The support console

The console is a demo UI. Its URL carries a Functions key (`?code=...`), it has no sign-in, and it acts as a single engineer (`SUPPORT_ENGINEER_NAME`).

- In production your real ticket system is the UI. If you keep a console, put it behind [App Service authentication](https://learn.microsoft.com/azure/app-service/overview-authentication-authorization) (Easy Auth) with Entra ID, and take the engineer's name from the signed-in user rather than a setting.
- Don't share console links; the key in them grants access to every console endpoint.

### Attachments and KB pages

`/api/files/{ticket}/{file}` (charts and draft KB articles) and `/api/kb/{id}` are anonymous so links in drafts open in one click. Attachment names include the ticket ID but aren't secret. For production, serve attachments from your ticket system's own storage with its access control, or from Blob Storage with short-lived user-delegation SAS links.

### The MCP key

The ticket-system connection uses the Functions MCP system key in a header. It's stored in Foundry, but it's still a shared secret.

- Prefer an MCP server that accepts **Entra ID** tokens, and create the connection with project managed identity or agent identity auth. Then there's no secret at all.
- If you keep a key, rotate it on a schedule and update the connection (`python scripts/setup.py` re-reads it).
- Put [API Management](https://learn.microsoft.com/azure/api-management/) in front of your MCP server for rate limits, IP filtering, and logging.

### Networking

Everything here uses public endpoints. For production, use private networking: a Foundry account with network isolation, private endpoints for Search and Storage, and a VNet-integrated Function App (or your ticket system behind a private endpoint). See [Configure private link for Foundry](https://learn.microsoft.com/azure/ai-foundry/how-to/configure-private-link).

### Memory

The demo uses one memory scope for all customers so a reset is simple. In production:

- **Scope memory per customer** (for example the customer ID), so one customer's facts can never surface in another customer's ticket.
- Keep the store's instructions about what never to store, and review them with your privacy team.
- Decide on retention, and how you'll honor deletion requests (delete the customer's scope).
- Don't wipe memory on a schedule like the demo reset does.

### Agent versions

The Case Manager uses the latest version of each prompt agent unless you pin one. That's convenient while you iterate and risky in production, where anyone with access to the project can publish a version. Pin versions with `*_AGENT_VERSION` settings, and roll forward only after the evals pass:

```bash
python evals/run_evals.py drafting --repeat 3
azd env set DRAFTING_AGENT_VERSION 5
azd deploy case-manager
```

### Guardrail strictness

The policy blocks at medium severity, and task adherence only annotates. Review the thresholds against your own traffic. Consider blocking on task adherence once you've seen what it flags, and add a blocklist for terms that should never appear in a customer email.

### Roles

The Bicep grants the deployer data-plane roles (Foundry User, search and storage data roles) so the setup script and evals can run. In production, give those to a deployment identity in CI/CD, not to people, and remove them from individuals after setup.

### Monitoring

Application Insights receives traces for every run. Add alerts for failed runs (`[Run stopped]` and `Run didn't finish` notes), guardrail hits, and model latency. Use [continuous evaluation](https://learn.microsoft.com/azure/ai-foundry/how-to/continuous-evaluation-agents) to score a sample of production responses over time.

### Data

The sample tickets, customers, and logs are synthetic. Before you point the agents at real tickets, review what customer data the models will see, where your Foundry resource and model deployments are located (the triage model uses a US Data Zone deployment), and your data processing agreements.

## Reporting a security issue

If you find a security issue in this sample, please don't open a public issue. Contact the repository owner privately.
