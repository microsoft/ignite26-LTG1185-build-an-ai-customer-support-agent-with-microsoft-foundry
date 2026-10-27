# Troubleshooting

## Deployment

**`InsufficientQuota` or `the capacity requested is not available`**
Your subscription doesn't have enough model quota in the region. Check **Foundry portal** > **Management center** > **Quota**, then either free up capacity, request more, or pick another region with `azd env set AZURE_LOCATION <region>` and run `azd up` again. The triage model uses a Data Zone deployment, which needs quota for `gpt-5.4-mini` with the **DataZoneStandard** SKU.

**Azure AI Search fails with "insufficient capacity" in your region**
Put Search in a different region. It talks to Foundry over the public endpoint, so the region doesn't need to match:

```bash
azd env set AZURE_SEARCH_LOCATION centralus
azd provision
```

**`AuthorizationFailed` on a role assignment**
The deployment creates role assignments, so you need **Owner** or **User Access Administrator** on the subscription (or the resource group, if you create it in advance). If you can't get that, ask an admin to run `azd provision` once.

**The setup step prints "you need Owner or User Access Administrator to grant this"**
The Case Manager and follow-up agent each get their own identity when they're deployed, and the setup script grants it the **Foundry User** role on the project. Ask someone with the right role to grant the printed principal ID that role, or run `python scripts/setup.py` as that person.

**`azd up` stops at the post-deploy step**
The setup script is safe to re-run. Fix the error it printed and run:

```bash
python scripts/setup.py
```

(On Windows, if the `.venv` it created exists, use `.venv\Scripts\python scripts\setup.py`.)

**"Indexing the KB articles failed"**
Usually a role assignment that hasn't propagated yet (Search reading Blob Storage, or calling the embedding model). Wait five minutes and re-run `python scripts/setup.py`.

## Running the agents

**A run takes longer than three minutes, or the ticket says "Run didn't finish"**
Foundry occasionally answers a request with a 408 timeout under load. The Case Manager retries each step, and the ticket system's watchdog restarts a run that stopped (up to twice). If a run still fails, press **Retry** in the console. Check the trace in the portal (agent > **Traces**) to see which step timed out.

**The console doesn't show the latest ticket state, or a dialog looks broken**
The console page is cached by the browser after an update. Hard-refresh with **Ctrl+Shift+R** (**Cmd+Shift+R** on macOS).

**The drafting agent ignores the customer's preferences, or mentions details from another ticket**
Memory learns from every run in the scope, including tests and evals. Reset it:

```bash
python scripts/reset_demo.py
```

The reset wipes the memory scope and reseeds the known facts. It takes one to five minutes for the memory store to finish saving the seed.

**My change to an agent's instructions doesn't show up**
The Case Manager looks up the latest version of each agent every five minutes. Wait, or check that you **published** a new version in the portal (saving a draft isn't enough). If you set a version pin (`*_AGENT_VERSION`), the pin wins; clear it with `azd env set DRAFTING_AGENT_VERSION ""` and `azd deploy case-manager`.

**The diagnostics chart is missing**
Code Interpreter sometimes skips the chart when the logs are small. The diagnosis still works. Re-run the ticket to try again.

## Logs

| What | Where |
|---|---|
| A whole run, step by step | Foundry portal > **Agents** > `case-manager` > **Traces**, or the `case-intake` routine's run history |
| The Case Manager container | Foundry portal > `case-manager` > **Logs**, or `azd ai agent monitor` |
| Ticket system, watchdog, dispatches | Azure portal > Function App > **Log stream**, or Application Insights > **Logs** (`traces` table) |
| Eval results | Foundry portal > **Evaluation**, or the link the eval script prints |

## Start over

```bash
azd down --purge
azd up
```
