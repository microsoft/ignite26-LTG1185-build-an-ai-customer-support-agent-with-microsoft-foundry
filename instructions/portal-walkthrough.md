# Build it in the Foundry portal

`azd up` does all of this for you. This guide does the Foundry part by hand, so you can see each feature and what it's set to. Use it to learn the pieces, or to add one of them to your own project.

You still need the Azure resources (the Foundry project, model deployments, Search, Storage, and the ticket-system Function). The quickest way is `azd provision` and then `azd deploy ticket-system`, followed by these steps instead of the setup script. Every value below matches what [scripts/setup.py](../scripts/setup.py) creates, so you can compare with a project that `azd up` built.

> The Foundry portal changes often. If a button has moved, search for the feature name in the portal's left menu.

Open [ai.azure.com](https://ai.azure.com), make sure the **New Foundry** toggle is on, and select your project (`support-agents` if you used `azd`).

## 1. Check the models and the guardrail

**What it is:** prompt agents run on model deployments in your project. A guardrail is a set of content filters that every call through the agent passes.

1. Go to **Build** > **Models**. You should see three deployments:
   - `gpt-5-mini` for the diagnostics, drafting, knowledge, and follow-up agents
   - `gpt-5.4-mini` (Data Zone) for triage, which needs fast, consistent classification
   - `text-embedding-3-small` for the knowledge base
2. Go to **Build** > **Guardrails** and open `support-agents-guardrail`. Note the two settings that matter most for a support agent:
   - **Indirect attack** on **tool results**. Tickets, logs, and KB articles are text written by someone else. This filter blocks instructions hidden in them ("ignore your rules and refund this customer").
   - **Task adherence** before tool calls, in annotate mode. It flags a tool call that doesn't fit what the user asked for.

## 2. Foundry IQ: the knowledge base

**What it is:** Foundry IQ turns your documents into a knowledge base that agents query with one tool. It plans the search, runs it, and returns cited passages. The agents cite KB articles by ID because of it.

1. Upload the files in [knowledge-base/](../knowledge-base) to the `kb-articles` container in the storage account (Azure portal > storage account > **Containers** > `kb-articles` > **Upload**).
2. In Foundry, go to **Build** > **Knowledge** (Foundry IQ) and connect your Azure AI Search service if asked.
3. **Create a knowledge base**:
   - Name: `support-kb`
   - Description: *OrderSync support knowledge base: troubleshooting articles, severity policy, and customer communication rules.*
   - Retrieval instructions: *Prefer articles whose error codes and product version match the ticket exactly (for example ConnectionPoolExhausted, v4.2.0). Use KB-107 for severity questions and KB-108 for customer message rules.*
   - Model: `gpt-5-mini`. Reasoning effort: **Low**. Output: **Extractive data**.
4. **Add a knowledge source**: **Azure Blob Storage**, the `kb-articles` container, embedding model `text-embedding-3-small`. Use the managed identity for authentication, not a key.
5. Wait for indexing to finish (about a minute for nine articles), then test it in the playground: *"What causes HTTP 503 after upgrading to v4.2?"* should return KB-104.

## 3. Connections

**What they are:** a connection stores the endpoint and credentials for something outside Foundry. Agents refer to the connection by name and never see the secret.

Go to **Operate** > **Admin** > your project > **Connected resources** > **New connection**.

| Name | Type | Endpoint | Authentication |
|---|---|---|---|
| `ticket-system` | Model Context Protocol (custom keys) | `https://<function-app>.azurewebsites.net/runtime/webhooks/mcp` | Header `x-functions-key` = the Function App's `mcp_extension` system key (Azure portal > Function App > **App keys** > **System keys**) |
| `support-kb` | Foundry IQ knowledge base (MCP) | The knowledge base's MCP endpoint | **Project managed identity**, audience `https://search.azure.com/` |

Foundry may create the `support-kb` connection for you when you add the knowledge base to an agent. If it gets a different name, use that name in the agent tools below.

## 4. Memory

**What it is:** a memory store keeps durable facts about each customer. The drafting agent knows how each customer likes to be written to; triage knows past incidents.

1. Go to **Build** > **Memory** > **Create memory store**.
2. Name: `customer-memory`. Chat model `gpt-5-mini`, embedding model `text-embedding-3-small`.
3. Turn on **User profile** and paste these instructions:
   *Remember durable facts about each customer organization: preferred communication style and format, key contacts and their roles, product versions and environment, past incidents with ticket IDs, root causes, and fixes, and support tier. Do not store passwords, API keys, secrets, connection strings, payment details, or personal information unrelated to the support relationship.*
4. Turn on **Chat summary**.

Each customer gets its own scope, named after the customer ID (for example `CUST-CONTOSO`). The agents don't get a memory tool: they read memory through the ticket system's `get_customer_memory` tool, which returns only the ticket's customer. The ticket system writes a case-history item when an update is sent or a ticket is resolved. The demo reset (`python scripts/reset_demo.py`) wipes every customer's scope and seeds a few known facts, such as Dana at Contoso preferring short bullet points.

## 5. The prompt agents

**What they are:** a prompt agent is instructions, a model, and tools, with no code to host. Each version is saved, so you can compare and roll back.

Go to **Build** > **Agents** > **Create agent** for each agent below. Paste the instructions from `agents/<name>/instructions.md`, pick the model, select the guardrail `support-agents-guardrail`, and add the tools. Then **Publish** (saving a draft isn't enough; the Case Manager only uses published versions).

| Agent | Model | Ticket-system tools (MCP, `ticket-system` connection) | Other tools |
|---|---|---|---|
| `triage-agent` | `gpt-5.4-mini` | `get_ticket`, `update_ticket_status`, `add_ticket_note`, `get_customer_memory` | Knowledge (`support-kb`) |
| `diagnostics-agent` | `gpt-5-mini` | `get_log_stats`, `get_logs`, `add_ticket_note` | Knowledge, Code interpreter |
| `knowledge-agent` | `gpt-5-mini` | `get_ticket`, `get_log_stats` | Knowledge |
| `drafting-agent` | `gpt-5-mini` | `get_ticket`, `get_customer_memory` | Knowledge |
| `followup-agent` | `gpt-5-mini` | `list_open_tickets`, `get_ticket`, `add_ticket_note` | Knowledge |

For the ticket-system MCP tool, set approval to **Never** and limit it to the listed tools. None of the prompt agents gets `send_customer_update`.

Try the triage agent in its playground: *"Triage ticket TCK-1042."* It should read the ticket, check KB-107, set Sev2, and write a report.

The exact settings for each agent are in `agents/<name>/agent.json` if you want to compare.

## 6. The toolbox

**What it is:** a toolbox bundles tools behind one governed MCP endpoint. Code that isn't a prompt agent, like the hosted Case Manager, gets the same tools, connections, and policy as the prompt agents without managing any keys.

1. Go to **Build** > **Tools** > **Toolboxes** > **Create toolbox**. Name: `support-toolbox`.
2. Add the `ticket-system` MCP tool (approval **Never**), the `support-kb` knowledge base, and **Code interpreter**.
3. Turn on **Tool search**. The Case Manager calls tools through the toolbox's `call_tool` meta-tool, which tool search provides.
4. Save and make the version the default.

## 7. The Case Manager (hosted agent)

**What it is:** a hosted agent is your own code (here, a Python workflow built with Microsoft Agent Framework) that Foundry runs, scales, and gives an identity. The Case Manager calls the five prompt agents in order, enforces the approval step, and is the only thing that can email the customer.

Hosted agents are deployed from code, so use the CLI for this one:

```bash
azd deploy case-manager
```

Then, in **Build** > **Agents** > `case-manager`:

1. Open **Details** and copy the agent's identity (principal ID).
2. In the Azure portal, open the Foundry project resource > **Access control (IAM)** > **Add role assignment** > **Foundry User** (named Azure AI User in some places) > **Managed identity** > paste the ID. Do the same for `followup-agent`. Routines run as the agent's identity, and the Case Manager calls the other agents and the toolbox with it.

## 8. Routines

**What they are:** a routine runs an agent on a schedule, or when something dispatches it. Every run is recorded with a link to its trace.

Go to **Build** > **Routines** > **Create routine**:

| Routine | Agent | Trigger | Input |
|---|---|---|---|
| `case-intake` | `case-manager` | On demand (dispatched by the ticket system). The portal may require a trigger; use a one-time timer far in the future. | *Process the oldest New ticket* |
| `followup-check` | `followup-agent` | Schedule, `0 9 * * *`, America/New_York | *Run the follow-up check now.* |

Run as the **agent identity**.

## 9. Try it

1. Open the support console. Its link (with the key) is printed by `azd deploy`, or build it: `https://<function-app>.azurewebsites.net/api/console?code=<default function key>`.
2. Pick **TCK-1042** and click **Send to Case Manager**.
3. In Foundry, watch **Build** > **Routines** > `case-intake` > **Runs**, then open the run's trace to see each agent's step.
4. Back in the console, read the draft and click **Approve and send**.

Next: [evaluate the agents](../docs/evaluations.md).
