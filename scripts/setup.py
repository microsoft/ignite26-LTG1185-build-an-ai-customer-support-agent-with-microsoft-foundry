"""Configure the Foundry side of the sample after `azd deploy`. Safe to run again: each step skips work that's done.

Runs automatically as the azd postdeploy hook. To run it by hand:   python scripts/setup.py

What it sets up, in order (each step matches a section in docs/portal-walkthrough.md):
  1. Ticket system keys   read the Functions MCP key so Foundry can call the ticket system's tools
  2. Foundry IQ           upload the KB articles, create the knowledge source and the support-kb knowledge base
  3. Connections          ticket-system (MCP, function key) and support-kb (Foundry IQ MCP, project identity)
  4. Customer memory      the customer-memory store: one scope per customer, written only by the ticket system
  5. Toolbox              support-toolbox: one MCP endpoint the Case Manager uses for every tool call
  6. Prompt agents        triage, diagnostics, drafting, knowledge, follow-up (agents/*/), each with the guardrail
  7. Agent identity       give the Case Manager's and follow-up agent's identities access to the project
  8. Routines             case-intake (the ticket system starts Case Manager runs through it) and a daily follow-up
  9. Demo data            load the sample tickets and seed customer memory
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path

from azure.identity import AzureCliCredential, AzureDeveloperCliCredential, ChainedTokenCredential

ROOT = Path(__file__).resolve().parent.parent
SEARCH_API = "2026-08-01-preview"
ARM = "https://management.azure.com"
ARM_CONNECTIONS_API = "2025-06-01"
FOUNDRY_USER_ROLE = "53ca6127-db72-4b80-b1b0-d745d6d5456d"  # Azure AI User, shown as Foundry User in the portal
PREVIEW = {
    "memory": "MemoryStores=V1Preview",
    "toolbox": "Toolboxes=V1Preview",
    "routine": "Routines=V1Preview",
}

KB_SOURCE = "ks-kb-articles"
KB_NAME = "support-kb"
MEMORY_STORE = "customer-memory"
TOOLBOX = "support-toolbox"
AGENTS = ["triage-agent", "diagnostics-agent", "drafting-agent", "knowledge-agent", "followup-agent"]


# ---------- plumbing ----------

def load_env() -> dict:
    """azd passes its environment to hooks. When run by hand, read it from `azd env get-values`."""
    env = dict(os.environ)
    if "FOUNDRY_PROJECT_ENDPOINT" not in env:
        out = subprocess.run(["azd", "env", "get-values"], capture_output=True, text=True, cwd=ROOT, shell=os.name == "nt")
        if out.returncode != 0:
            sys.exit("Run `azd up` first (or `azd env select <name>`): no azd environment values found.")
        for line in out.stdout.splitlines():
            if "=" in line:
                key, value = line.split("=", 1)
                env.setdefault(key, value.strip().strip('"'))
    missing = [k for k in ("FOUNDRY_PROJECT_ENDPOINT", "AZURE_SUBSCRIPTION_ID", "AZURE_RESOURCE_GROUP", "FUNCTION_APP_NAME") if not env.get(k)]
    if missing:
        sys.exit(f"Missing azd environment values: {', '.join(missing)}. Run `azd provision` first.")
    return env


ENV = load_env()
PROJECT = ENV["FOUNDRY_PROJECT_ENDPOINT"].rstrip("/")
SUB, RG = ENV["AZURE_SUBSCRIPTION_ID"], ENV["AZURE_RESOURCE_GROUP"]
PROJECT_ID = ENV["AZURE_AI_PROJECT_ID"]
SEARCH = ENV["SEARCH_ENDPOINT"].rstrip("/")
TICKETS = ENV["TICKET_SYSTEM_URL"].rstrip("/")
FUNCTION_ID = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Web/sites/{ENV['FUNCTION_APP_NAME']}"
STORAGE_ID = ENV["STORAGE_ACCOUNT_ID"]

_tenant = ENV.get("AZURE_TENANT_ID") or None
credential = ChainedTokenCredential(AzureDeveloperCliCredential(tenant_id=_tenant), AzureCliCredential(tenant_id=_tenant))
_tokens: dict[str, tuple[float, str]] = {}


def token(scope: str) -> str:
    cached = _tokens.get(scope)
    if cached and cached[0] - time.time() > 120:
        return cached[1]
    t = credential.get_token(scope)
    _tokens[scope] = (t.expires_on, t.token)
    return t.token


class HttpError(Exception):
    def __init__(self, status: int, body: str, url: str):
        super().__init__(f"HTTP {status} from {url.split('?')[0]}: {body[:600]}")
        self.status, self.body = status, body


def http(method: str, url: str, scope: str | None = None, body=None, headers: dict | None = None,
         raw: bytes | None = None, retries: int = 4, timeout: int = 120) -> dict:
    """JSON over HTTPS with an Entra token. Retries throttling and transient Foundry errors (408/429/5xx)."""
    for attempt in range(1, retries + 1):
        h = {"Content-Type": "application/json", **(headers or {})}
        if scope:
            h["Authorization"] = "Bearer " + token(scope)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        try:
            with urllib.request.urlopen(urllib.request.Request(url, data=data, headers=h, method=method), timeout=timeout) as r:
                text = r.read().decode() or "{}"
                return json.loads(text) if text.lstrip().startswith(("{", "[")) else {"text": text}
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")
            if e.code in (408, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(5 * attempt)
                continue
            raise HttpError(e.code, detail, url) from None
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < retries:
                time.sleep(5 * attempt)
                continue
            raise HttpError(0, str(e), url) from None
    raise AssertionError("unreachable")


def foundry(method: str, path: str, body=None, preview: str | None = None, **kw) -> dict:
    headers = {"Foundry-Features": preview} if preview else None
    sep = "&" if "?" in path else "?"
    return http(method, f"{PROJECT}{path}{sep}api-version=v1", "https://ai.azure.com/.default", body, headers, **kw)


def arm(method: str, path: str, api: str, body=None) -> dict:
    return http(method, f"{ARM}{path}?api-version={api}", "https://management.azure.com/.default", body)


def search(method: str, path: str, body=None) -> dict:
    return http(method, f"{SEARCH}{path}?api-version={SEARCH_API}", "https://search.azure.com/.default", body)


def exists(fn, *args) -> dict | None:
    try:
        return fn("GET", *args)
    except HttpError as e:
        if e.status == 404:
            return None
        raise


def step(title: str) -> None:
    print(f"\n==> {title}", flush=True)


def fill(text: str, values: dict) -> str:
    """Replace ${NAME} placeholders in the agent templates with azd environment values."""
    def sub(m: re.Match) -> str:
        if m.group(1) not in values:
            sys.exit(f"agents template needs {m.group(1)}, which is not in the azd environment.")
        return values[m.group(1)]
    return re.sub(r"\$\{([A-Z0-9_]+)\}", sub, text)


def contains(actual, desired) -> bool:
    """True if every value in `desired` is already present in `actual` (the service may add defaults)."""
    if isinstance(desired, dict):
        return isinstance(actual, dict) and all(contains(actual.get(k), v) for k, v in desired.items())
    if isinstance(desired, list):
        return isinstance(actual, list) and len(actual) == len(desired) and all(contains(a, d) for a, d in zip(actual, desired))
    return actual == desired


# ---------- 1. Ticket system keys ----------

def function_keys() -> dict:
    """The MCP extension's system key only exists once the Functions host has started, so wake it and wait."""
    step("Ticket system: reading the MCP key")
    deadline = time.time() + 300
    while True:
        try:
            urllib.request.urlopen(f"{TICKETS}/api/kb", timeout=60).read()
        except Exception:  # noqa: BLE001 - only waking the host
            pass
        keys = arm("POST", f"{FUNCTION_ID}/host/default/listkeys", "2024-04-01")
        if keys.get("systemKeys", {}).get("mcp_extension") and keys.get("functionKeys", {}).get("default"):
            print("    found the mcp_extension system key")
            return keys
        if time.time() > deadline:
            sys.exit("The ticket system's mcp_extension key never appeared. Check the Function App's logs, then rerun setup.")
        print("    waiting for the Functions host to start...")
        time.sleep(15)


# ---------- 2. Foundry IQ ----------

def upload_kb_articles() -> None:
    step("Foundry IQ: uploading KB articles to Blob Storage")
    account = ENV["STORAGE_ACCOUNT_NAME"]
    for path in sorted((ROOT / "knowledge-base").glob("KB-*.md")):
        url = f"https://{account}.blob.core.windows.net/kb-articles/{urllib.parse.quote(path.name)}"
        http("PUT", url, "https://storage.azure.com/.default", raw=path.read_bytes(), headers={
            "x-ms-blob-type": "BlockBlob", "x-ms-version": "2023-11-03", "Content-Type": "text/markdown; charset=utf-8"})
        print(f"    {path.name}")


def knowledge_base() -> None:
    step("Foundry IQ: knowledge source and knowledge base")
    openai = ENV["AZURE_OPENAI_ENDPOINT"].rstrip("/")
    if exists(search, f"/knowledgesources/{KB_SOURCE}"):
        print(f"    knowledge source {KB_SOURCE} exists")
    else:
        # Search reads the blobs and calls the embedding model with its own managed identity: no keys anywhere.
        search("PUT", f"/knowledgesources/{KB_SOURCE}", {
            "name": KB_SOURCE,
            "kind": "azureBlob",
            "description": "Official OrderSync KB articles: troubleshooting fixes, severity definitions, customer communication rules.",
            "azureBlobParameters": {
                "connectionString": f"ResourceId={STORAGE_ID};",
                "containerName": "kb-articles",
                "ingestionParameters": {
                    "contentExtractionMode": "minimal",
                    "embeddingModel": {"kind": "azureOpenAI", "azureOpenAIParameters": {
                        "resourceUri": openai,
                        "deploymentId": ENV["EMBEDDING_MODEL_DEPLOYMENT"],
                        "modelName": ENV["EMBEDDING_MODEL_DEPLOYMENT"],
                    }},
                },
            },
        })
        print(f"    created knowledge source {KB_SOURCE} (Search creates the index, indexer, and skillset)")
    search("PUT", f"/knowledgebases/{KB_NAME}", {
        "name": KB_NAME,
        "description": "OrderSync support knowledge base: troubleshooting articles, severity policy, and customer communication rules.",
        "retrievalInstructions": (
            "Prefer articles whose error codes and product version match the ticket exactly "
            "(for example ConnectionPoolExhausted, v4.2.0). Use KB-107 for severity questions and KB-108 for customer message rules."
        ),
        "outputMode": "extractiveData",
        "knowledgeSources": [{"name": KB_SOURCE}],
        "models": [{"kind": "azureOpenAI", "azureOpenAIParameters": {
            "resourceUri": openai,
            "deploymentId": ENV["CHAT_MODEL_DEPLOYMENT"],
            "modelName": ENV["CHAT_MODEL_DEPLOYMENT"],
        }}],
        "retrievalReasoningEffort": {"kind": "low"},
    })
    print(f"    knowledge base {KB_NAME} is up to date")
    indexer = f"{KB_SOURCE}-indexer"
    search("POST", f"/indexers/{indexer}/run")
    deadline = time.time() + 600
    while time.time() < deadline:
        last = (search("GET", f"/indexers/{indexer}/status").get("lastResult") or {})
        if last.get("status") == "success":
            print(f"    indexed {last.get('itemsProcessed', 0)} changed articles")
            return
        if last.get("status") in ("transientFailure", "persistentFailure"):
            sys.exit(f"Indexing the KB articles failed: {last.get('errorMessage') or last.get('errors')}")
        time.sleep(10)
    print("    indexing is still running; it will finish in the background")


# ---------- 3. Connections ----------

def connections(mcp_key: str) -> None:
    step("Connections: ticket-system and support-kb")
    # The ticket system authenticates callers with the Functions MCP system key, sent as a header.
    arm("PUT", f"{PROJECT_ID}/connections/ticket-system", ARM_CONNECTIONS_API, {"properties": {
        "category": "RemoteTool",
        "target": ENV["TICKET_SYSTEM_MCP_URL"],
        "authType": "CustomKeys",
        "credentials": {"keys": {"x-functions-key": mcp_key}},
        "metadata": {"type": "custom_MCP"},
        "isSharedToAll": True,
    }})
    print("    ticket-system  (custom MCP, key in a header)")
    # Agents reach the Foundry IQ knowledge base with the project's managed identity (Search Index Data Reader).
    arm("PUT", f"{PROJECT_ID}/connections/{KB_NAME}", ARM_CONNECTIONS_API, {"properties": {
        "category": "RemoteTool",
        "target": ENV["KNOWLEDGE_BASE_MCP_URL"],
        "authType": "ProjectManagedIdentity",
        "audience": "https://search.azure.com/",
        "metadata": {"type": "knowledgeBase_MCP", "knowledgeBaseName": KB_NAME},
        "isSharedToAll": True,
    }})
    print(f"    {KB_NAME}     (Foundry IQ MCP, project managed identity)")


# ---------- 4. Customer memory ----------

def memory_store() -> None:
    """Agents read the store through the ticket system's get_customer_memory tool, and the ticket system writes
    items directly (seed facts at reset, case history when an update is sent or a ticket is resolved). No agent
    has the memory_search tool, because that tool saves facts from every conversation into the scope."""
    step("Customer memory")
    if exists(foundry, f"/memory_stores/{MEMORY_STORE}", None, PREVIEW["memory"]):
        print(f"    {MEMORY_STORE} exists")
        return
    foundry("POST", "/memory_stores", {
        "name": MEMORY_STORE,
        "description": "Per-customer support context: communication preferences, key contacts, and past incidents.",
        "definition": {
            "kind": "default",
            "chat_model": ENV["CHAT_MODEL_DEPLOYMENT"],
            "embedding_model": ENV["EMBEDDING_MODEL_DEPLOYMENT"],
            "options": {
                "user_profile_enabled": True,
                "user_profile_details": (
                    "Remember durable facts about each customer organization: preferred communication style and format, "
                    "key contacts and their roles, product versions and environment, past incidents with ticket IDs, root "
                    "causes, and fixes, and support tier. Do not store passwords, API keys, secrets, connection strings, "
                    "payment details, or personal information unrelated to the support relationship."
                ),
                "chat_summary_enabled": True,
            },
        },
    }, PREVIEW["memory"])
    print(f"    created {MEMORY_STORE}")


# ---------- 5. Toolbox ----------

def toolbox() -> None:
    step("Toolbox")
    desired = {
        "description": "Contoso support tools: ticket system, knowledge base, code interpreter",
        "tools": [
            # Tool search exposes the toolbox through two meta-tools (tool_search, call_tool) instead of every tool
            # schema at once. The Case Manager calls ticket tools through call_tool.
            {"type": "toolbox_search", "name": "toolbox_search"},
            {"type": "mcp", "server_label": "ticket-system", "server_url": ENV["TICKET_SYSTEM_MCP_URL"],
             "project_connection_id": "ticket-system", "require_approval": "never"},
            {"type": "mcp", "server_label": KB_NAME, "server_url": ENV["KNOWLEDGE_BASE_MCP_URL"],
             "project_connection_id": KB_NAME, "require_approval": "never"},
            {"type": "code_interpreter", "container": {"type": "auto"}},
        ],
    }
    current = exists(foundry, f"/toolboxes/{TOOLBOX}", None, PREVIEW["toolbox"])
    if current:
        default = current.get("default_version")
        live = foundry("GET", f"/toolboxes/{TOOLBOX}/versions/{default}", None, PREVIEW["toolbox"])
        if contains(live.get("tools"), desired["tools"]):
            print(f"    {TOOLBOX} v{default} is up to date")
            return
    version = foundry("POST", f"/toolboxes/{TOOLBOX}/versions", desired, PREVIEW["toolbox"])["version"]
    foundry("PATCH", f"/toolboxes/{TOOLBOX}", {"default_version": str(version)}, PREVIEW["toolbox"])
    print(f"    {TOOLBOX} v{version} is now the default version")


# ---------- 6. Prompt agents ----------

def prompt_agents() -> dict:
    step("Prompt agents")
    values = {k: ENV[k] for k in ("RAI_POLICY_ID", "CHAT_MODEL_DEPLOYMENT", "TRIAGE_MODEL_DEPLOYMENT",
                                  "TICKET_SYSTEM_MCP_URL", "KNOWLEDGE_BASE_MCP_URL")}
    versions = {}
    for name in AGENTS:
        folder = ROOT / "agents" / name
        spec = json.loads(fill((folder / "agent.json").read_text(encoding="utf-8"), values))
        spec["definition"]["instructions"] = (folder / "instructions.md").read_text(encoding="utf-8").strip()
        current = exists(foundry, f"/agents/{name}")
        latest = (current or {}).get("versions", {}).get("latest", {})
        if latest and contains(latest.get("definition"), spec["definition"]):
            versions[name] = latest["version"]
            print(f"    {name} v{latest['version']} is up to date")
            continue
        created = foundry("POST", f"/agents/{name}/versions", {"description": spec["description"], "definition": spec["definition"]})
        versions[name] = created["version"]
        print(f"    {name} v{created['version']} created")
    return versions


# ---------- 7. Agent identity ----------

def grant_agent_identity(agent: str) -> None:
    """Each deployed agent gets its own Entra identity. Routines and the Case Manager's calls to the other
    agents and the toolbox run as that identity, so it needs Foundry User on the project."""
    found = exists(foundry, f"/agents/{agent}")
    principal = ((found or {}).get("instance_identity") or {}).get("principal_id")
    if not principal:
        print(f"    {agent}: no agent identity yet (deploy it first)")
        return
    assignment = uuid.uuid5(uuid.NAMESPACE_URL, f"{PROJECT_ID}|{principal}|{FOUNDRY_USER_ROLE}")
    try:
        arm("PUT", f"{PROJECT_ID}/providers/Microsoft.Authorization/roleAssignments/{assignment}", "2022-04-01", {"properties": {
            "roleDefinitionId": f"/subscriptions/{SUB}/providers/Microsoft.Authorization/roleDefinitions/{FOUNDRY_USER_ROLE}",
            "principalId": principal,
            "principalType": "ServicePrincipal",
        }})
        print(f"    {agent}: Foundry User on the project")
    except HttpError as e:
        if e.status == 409:
            print(f"    {agent}: already has access")
        elif e.status == 403:
            print(f"    {agent}: you need Owner or User Access Administrator to grant this. Ask an admin to give the agent "
                  f"identity {principal} the Foundry User role on the project.")
        else:
            raise


# ---------- 8. Routines ----------

ROUTINES = {
    # The ticket system dispatches this routine on demand (console "Run" button, new tickets, approvals), so every
    # Case Manager run shows up in the routine's run history in the portal, linked to its trace. A routine needs a
    # trigger, so it gets a one-time timer far in the future; the dispatches are what actually start runs.
    "case-intake": {
        "description": "Starts a Case Manager run for a ticket. Dispatched by the ticket system.",
        "enabled": True,
        "triggers": {"default": {"type": "timer", "at": 4070908800, "time_zone": "UTC"}},
        "action": {"type": "invoke_agent_responses_api", "agent_endpoint_id": "case-manager",
                   "input": "Process the oldest New ticket"},
        "authorization": {"identity": "agent"},
    },
    "followup-check": {
        "description": "Every morning, the follow-up agent checks open tickets that are waiting on the customer or overdue.",
        "enabled": True,
        "triggers": {"default": {"type": "schedule", "cron_expression": "0 9 * * *", "time_zone": "America/New_York"}},
        "action": {"type": "invoke_agent_responses_api", "agent_name": "followup-agent", "input": "Run the follow-up check now."},
        "authorization": {"identity": "agent"},
    },
}


def routines() -> None:
    step("Routines")
    for name, body in ROUTINES.items():
        foundry("PUT", f"/routines/{name}", {"name": name, **body}, PREVIEW["routine"])
        print(f"    {name}")


# ---------- 9. Demo data ----------

def demo_data(function_key: str) -> None:
    step("Demo data: sample tickets and customer memory")
    result = http("POST", f"{TICKETS}/api/demo/reset?run=false", headers={"x-functions-key": function_key}, timeout=180)
    print(f"    {result.get('tickets', result.get('ticket_count', 'sample'))} tickets loaded. {result.get('memory', '')}")


def main() -> None:
    keys = function_keys()
    upload_kb_articles()
    knowledge_base()
    connections(keys["systemKeys"]["mcp_extension"])
    memory_store()
    toolbox()
    versions = prompt_agents()
    step("Agent identity")
    grant_agent_identity("case-manager")
    grant_agent_identity("followup-agent")
    routines()
    demo_data(keys["functionKeys"]["default"])

    account, project = ENV.get("AZURE_AI_ACCOUNT_NAME", ""), ENV.get("AZURE_AI_PROJECT_NAME", "")
    print("\n" + "=" * 78)
    print("Setup complete.")
    print(f"  Support console:  {TICKETS}/api/console?code={keys['functionKeys']['default']}")
    print("                    (the code is a function key: treat the link like a password)")
    print(f"  Foundry portal:   https://ai.azure.com  >  {account} / {project}  >  Agents")
    print("  Agent versions:   " + ", ".join(f"{k} v{v}" for k, v in versions.items()))
    print("  Try it:           open the console, pick TCK-1042, and click Send to Case Manager.")
    print("=" * 78)


if __name__ == "__main__":
    main()
