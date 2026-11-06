# Connect your own ticket system

The sample's ticket system is a small Azure Function, but the agents never see it directly. They see eight **MCP tools**. Put the same eight tools in front of ServiceNow, Zendesk, Dynamics 365 Customer Service, Jira Service Management, or your own system, and the workflow stays the same: diagnose, draft, a person approves.

## What you change, and what stays

**What stays:**
- the Case Manager workflow and its routing in code
- the approval gate
- the five agents' jobs
- the tool contract below

**What you change:**

| # | Change | Where | Details |
|---|---|---|---|
| 1 | **Start runs from your system.** Give it an Entra identity (managed identity or service principal) with the **Foundry User** role on the project. Have a business rule, trigger, or flow dispatch the `case-intake` routine. | Your ticket system | [Start a run from your system](#start-a-run-from-your-system) |
| 2 | **Swap the connector.** Expose your system as the eight MCP tools and connect them as `ticket-system`. | Your MCP server, `scripts/setup.py`, `agents/*/agent.json` | [The contract](#the-contract), [Connect it to Foundry](#connect-it-to-foundry) |
| 3 | **Load your knowledge into Foundry IQ.** | `knowledge-base/` | Replace the files in `knowledge-base/` with your KB articles, runbooks, and resolved cases, then rerun `python scripts/setup.py`. It uploads them to the `kb-articles` container behind the `support-kb` knowledge base. You can also add your own source, such as SharePoint, to `support-kb` in the Foundry portal. |
| 4 | **Fit the prompts to your product.** | `agents/*/instructions.md` | The instructions name the sample's product (Contoso OrderSync) and three articles: KB-107 (severity definitions), KB-108 (communication guidelines), and KB-109 (log collection guide for customers). Replace the product name, point to your own equivalents of those three articles, then rerun `python scripts/setup.py` to publish new agent versions. The steps, rules, and output formats stay the same. |
| 5 | **Match your ID formats.** | Top of `src/case-manager/main.py` | Set `TICKET_ID` to your ticket ID format (for example `r"\bINC\d{7}\b"` for ServiceNow), `KB_ID` to your article ID format, and `CUSTOMER_KBS` to the articles customers may see. A message without a recognizable ticket ID is refused, not guessed. |
| 6 | **Feed customer memory.** | Your ticket system or CRM | Agents only read the `customer-memory` store. In the sample, the ticket system seeds a profile for each customer and adds case history when a ticket resolves (`_record_case_history` in `function_app.py`). Do the same from your system, with one scope per customer ID. |
| 7 | **Point the log tools at your logs.** | Your MCP server | `get_logs` and `get_log_stats` can query Application Insights, Splunk, or Datadog for the ticket's customer and time window. |
| 8 | **Put approval where your engineers work.** | Your ticket system | An **Approve** button dispatches `approve <ticket id> by <engineer>`. The sample's console is only a stand-in. |

Start with 1 to 3 on a single ticket type, then add the rest.

## The contract

Your MCP server needs these tools. Keep the names and arguments the same and the agent instructions keep working. If you rename them, update the `allowed_tools` lists in `agents/*/agent.json` and the tool names at the top of `src/case-manager/main.py`.

| Tool | Arguments | Returns | Used by |
|---|---|---|---|
| `get_ticket` | `ticket_id` | Ticket fields, customer and contact, every note and customer update | triage, drafting, knowledge, follow-up, Case Manager |
| `get_logs` | `ticket_id`, `max_lines` | Attached log lines (timestamp, level, service, message) | diagnostics |
| `get_log_stats` | `ticket_id` | Counts by level and error type, first and last error, the deployment event, errors per hour | diagnostics, knowledge |
| `add_ticket_note` | `ticket_id`, `note` | Confirmation. Notes are **internal only** | triage, diagnostics, follow-up, Case Manager |
| `update_ticket_status` | `ticket_id`, `status`, `severity` (optional) | The updated ticket | triage, Case Manager |
| `list_open_tickets` | `status` (optional) | Open tickets with hours since last update | follow-up |
| `send_customer_update` | `ticket_id`, `message` | Confirmation. **Emails the customer** | Case Manager only, after approval |
| `attach_file` | `ticket_id`, `filename`, `content_base64` | A link to the file | Case Manager (diagnostics chart) |

The descriptions in [function_app.py](../src/ticket-system/function_app.py) are part of the contract. The model reads them to decide when to call a tool, so copy them when you write your own.

Two rules worth keeping when you map these to a real system:

- **Only one tool reaches the customer.** `send_customer_update` should be the only path to a customer-visible comment or email, and only the Case Manager can call it. Prompt agents can't: their `allowed_tools` lists don't include it.
- **Notes are internal.** Map `add_ticket_note` to your system's internal or private work notes, never to a public comment.

## How to build the server

Pick whichever is easiest for you:

- **Azure Functions MCP extension** (what this sample does). Each tool is a function with an `@app.mcp_tool_trigger` decorator. Replace the bodies in `tickets_core.py` with calls to your system's REST API, and the rest of the sample stays the same.
- **Any MCP server** that supports the Streamable HTTP transport, in any language. See the [MCP SDKs](https://modelcontextprotocol.io/docs/sdk).
- **An existing MCP server for your product**, if your vendor ships one. Check that it offers the tools above (or map them), and that you can limit which tools each agent sees.

Typical mappings:

| Tool | ServiceNow | Zendesk | Dynamics 365 Customer Service |
|---|---|---|---|
| `get_ticket` | Table API `incident` + `sys_journal_field` | `GET /tickets/{id}` + `/comments` | `incidents({id})` + notes and activities |
| `add_ticket_note` | `work_notes` | Comment with `public: false` | Note (annotation) |
| `update_ticket_status` | `state`, `impact`/`urgency` | `status`, `priority` | `statuscode`, `prioritycode` |
| `send_customer_update` | `comments` (customer-visible) | Comment with `public: true` | Email activity to the contact |
| `attach_file` | Attachment API | Uploads API, then a comment | Note with a file |

Logs usually live somewhere else, such as Application Insights, Splunk, or Datadog. `get_logs` and `get_log_stats` can query that system with the ticket's customer and time window, rather than reading attachments.

## Connect it to Foundry

1. **Create a connection.** In the Foundry portal, go to your project > **Management center** > **Connected resources** > **New connection** > **Model Context Protocol** with custom keys. Name it `ticket-system` so the agents find it without changes, and store your server's key as a header (the sample uses `x-functions-key`). Agents never see the key; Foundry adds it to each call. To script it instead, change the target and key in `connections()` in [scripts/setup.py](../scripts/setup.py).
2. **Point the agents and the toolbox at it.** Replace `TICKET_SYSTEM_MCP_URL` in the `ticket-system` tool of each `agents/*/agent.json` file and in `toolbox()` in `scripts/setup.py` (both read the sample's Function URL from the azd environment), then run `python scripts/setup.py`. The Case Manager calls ticket tools through the toolbox as `ticket-system___<tool>`, so keep the server label `ticket-system`.
3. **Use OAuth if your system needs it.** For per-user access (the agent acts as the signed-in engineer), use an MCP connection with OAuth identity passthrough instead of a shared key. See [Connect to MCP servers](https://learn.microsoft.com/azure/ai-foundry/agents/how-to/tools/model-context-protocol) in the Foundry docs.

## Start a run from your system

In the sample, the ticket system starts the Case Manager by dispatching the `case-intake` routine with a message like `New ticket TCK-1042`. Do the same from your system:

- **ServiceNow**: a Business Rule or Flow Designer action on incident insert.
- **Zendesk**: a trigger with a webhook.
- **Dynamics 365**: a Power Automate flow on case create.

Each one calls:

```http
POST {project-endpoint}/routines/case-intake:dispatch_async?api-version=v1
Authorization: Bearer <token for https://ai.azure.com>
Content-Type: application/json

{ "payload": { "type": "invoke_agent_responses_api", "input": "New ticket INC0012345" } }
```

Use a managed identity or service principal with the **Foundry User** role on the project. The approval decision goes back the same way (`approve INC0012345 by <engineer>`). See `_send_routine` in [function_app.py](../src/ticket-system/function_app.py) for the retry logic worth copying: Foundry occasionally answers with a 408 or 429, and a repeated dispatch is safe because the Case Manager runs each run token only once.

If you'd rather keep the approval inside your ticket system's UI, have its **Approve** button dispatch the routine with that message and stop there. The console in this sample exists only because the demo needs a screen.
