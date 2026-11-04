"""Mock ticket service: "your existing ticketing system" exposed to Microsoft Foundry over MCP.

MCP endpoint (Streamable HTTP): https://<function-app>.azurewebsites.net/runtime/webhooks/mcp
Auth: send the `mcp_extension` system key in the `x-functions-key` header.
"""

import json
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor

import azure.functions as func

from tickets_core import CLOSED_STATUSES, RUN_TOKEN, TicketService, new_run_token, to_json, with_token

app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)
_cred = None


def _credential():
    """The Function App's managed identity in Azure (system-assigned unless TICKETS_IDENTITY_CLIENT_ID is set)."""
    global _cred
    if _cred is None:
        if os.getenv("WEBSITE_INSTANCE_ID"):
            from azure.identity import ManagedIdentityCredential

            client_id = os.getenv("TICKETS_IDENTITY_CLIENT_ID")
            _cred = ManagedIdentityCredential(client_id=client_id) if client_id else ManagedIdentityCredential()
        else:
            from azure.identity import DefaultAzureCredential

            _cred = DefaultAzureCredential()
    return _cred


class TableStore:
    """Azure Table Storage implementation of the store used by TicketService."""

    def __init__(self):
        from azure.data.tables import TableServiceClient, UpdateMode

        self._mode = UpdateMode.REPLACE
        conn = os.getenv("TICKETS_STORAGE_CONNECTION_STRING")
        if conn:
            self._svc = TableServiceClient.from_connection_string(conn)
        else:
            endpoint = os.environ["TICKETS_TABLE_ENDPOINT"]  # e.g. https://<storage-account>.table.core.windows.net
            self._svc = TableServiceClient(endpoint=endpoint, credential=_credential())
        self._ready = set()

    def _table(self, name):
        if name not in self._ready:
            self._svc.create_table_if_not_exists(name)
            self._ready.add(name)
        return self._svc.get_table_client(name)

    def upsert(self, table, entity):
        self._table(table).upsert_entity(entity, mode=self._mode)

    def get(self, table, pk, rk):
        from azure.core.exceptions import ResourceNotFoundError

        try:
            return dict(self._table(table).get_entity(pk, rk))
        except ResourceNotFoundError:
            return None

    def query_partition(self, table, pk):
        return [dict(e) for e in self._table(table).query_entities("PartitionKey eq @pk", parameters={"pk": pk})]

    def list_all(self, table):
        return [dict(e) for e in self._table(table).list_entities()]

    def clear(self, table):
        client = self._table(table)
        for e in client.list_entities(select=["PartitionKey", "RowKey"]):
            client.delete_entity(e["PartitionKey"], e["RowKey"])


_service = None


def service() -> TicketService:
    global _service
    if _service is None:
        _service = TicketService(TableStore())
    return _service


def _args(context: str) -> dict:
    return json.loads(context).get("arguments") or {}


def _run(tool, fn):
    try:
        return to_json(fn())
    except ValueError as e:
        return to_json({"error": str(e)})
    except Exception:
        logging.exception("Tool %s failed", tool)
        return to_json({"error": f"The ticket system could not complete '{tool}'. Try again."})


def _props(*props):
    return json.dumps([
        {"propertyName": name, "propertyType": ptype, "description": desc, "isRequired": required}
        for name, ptype, desc, required in props
    ])


TICKET_ID = ("ticket_id", "string", "The ticket ID, for example TCK-1042.", True)


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_ticket",
    description="Get a support ticket's details, customer contact information, and all notes and customer updates so far.",
    tool_properties=_props(TICKET_ID),
)
def get_ticket(context) -> str:
    a = _args(context)
    return _run("get_ticket", lambda: service().get_ticket(a.get("ticket_id")))


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_logs",
    description="Get the diagnostic log lines attached to a support ticket (timestamp, level, service, message).",
    tool_properties=_props(
        TICKET_ID,
        ("max_lines", "integer", "Maximum number of log lines to return (20-400, default 200).", False),
    ),
)
def get_logs(context) -> str:
    a = _args(context)
    return _run("get_logs", lambda: service().get_logs(a.get("ticket_id"), a.get("max_lines") or 200))


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_log_stats",
    description="Get a pre-computed analysis of a ticket's logs: exact counts by level and error type, first and last error, the change/deployment event, errors per hour, the peak hour, and sample log lines. Use this for counts and charts instead of counting raw log lines.",
    tool_properties=_props(TICKET_ID),
)
def get_log_stats(context) -> str:
    a = _args(context)
    return _run("get_log_stats", lambda: service().get_log_stats(a.get("ticket_id")))


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="add_ticket_note",
    description="Add an INTERNAL note to a ticket (findings, analysis, next steps). Internal notes are never shown to the customer.",
    tool_properties=_props(
        TICKET_ID,
        ("note", "string", "The note text.", True),
        ("author", "string", "Who is writing the note, for example triage-agent.", False),
    ),
)
def add_ticket_note(context) -> str:
    a = _args(context)
    return _run("add_ticket_note", lambda: service().add_ticket_note(a.get("ticket_id"), a.get("note"), a.get("author") or "agent"))


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="update_ticket_status",
    description="Change a ticket's status and optionally its severity. Statuses: New, Triaged, In progress, Awaiting customer, Escalated, Resolved, Closed. Severities: Sev1 (critical) to Sev4 (low).",
    tool_properties=_props(
        TICKET_ID,
        ("status", "string", "New status: New, Triaged, In progress, Awaiting customer, Escalated, Resolved, or Closed.", True),
        ("severity", "string", "Optional severity: Sev1, Sev2, Sev3, or Sev4.", False),
    ),
)
def update_ticket_status(context) -> str:
    a = _args(context)

    def update():
        result = service().update_ticket_status(a.get("ticket_id"), a.get("status"), a.get("severity"))
        if result["status"] in CLOSED_STATUSES:
            _record_case_history(result["ticket_id"], "resolved")
        return result

    return _run("update_ticket_status", update)


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="get_customer_memory",
    description="Read what we know about the ticket's customer: contacts, communication preferences, and past incidents. Read-only: the ticket system records case history itself.",
    tool_properties=_props(TICKET_ID),
)
def get_customer_memory(context) -> str:
    a = _args(context)

    def read():
        customer = service().get_ticket(a.get("ticket_id"))["customer"]
        items = _memory_items(customer["customer_id"])
        return {
            "customer": customer["name"],
            "memories": [{"kind": i.get("kind"), "content": i.get("content")} for i in items]
            or "No memories yet for this customer.",
        }

    return _run("get_customer_memory", read)


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="list_open_tickets",
    description="List tickets that are not Resolved or Closed, oldest update first, with hours since the last update. Use it to find cases that need a follow-up.",
    tool_properties=_props(("status", "string", "Optional status filter, for example 'Awaiting customer'.", False)),
)
def list_open_tickets(context) -> str:
    a = _args(context)
    return _run("list_open_tickets", lambda: service().list_open_tickets(a.get("status")))


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="send_customer_update",
    description="Send a CUSTOMER-FACING update to the ticket's customer contact. Only call this after a human support engineer has approved the exact message.",
    tool_properties=_props(TICKET_ID, ("message", "string", "The exact, human-approved message to send to the customer.", True)),
)
def send_customer_update(context) -> str:
    a = _args(context)

    def send():
        result = service().send_customer_update(a.get("ticket_id"), a.get("message"))
        _record_case_history(result["ticket_id"], "update_sent")
        return result

    return _run("send_customer_update", send)


@app.mcp_tool_trigger(
    arg_name="context",
    tool_name="attach_file",
    description="Attach a file, such as a chart image (.png), to a ticket. Returns a link to view the file.",
    tool_properties=_props(
        TICKET_ID,
        ("filename", "string", "File name, for example errors_by_hour.png.", True),
        ("content_base64", "string", "The file content, base64 encoded.", True),
    ),
)
def attach_file(context) -> str:
    a = _args(context)

    def attach():
        result = service().attach_file(a.get("ticket_id"), a.get("filename"), a.get("content_base64"))
        result["url"] = f"{_public_base()}{result.pop('path')}"
        return result

    return _run("attach_file", attach)


def _public_base() -> str:
    return f"https://{os.environ.get('WEBSITE_HOSTNAME', 'localhost:7071')}"


@app.route(route="files/{ticket_id}/{filename}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def get_file(req: func.HttpRequest) -> func.HttpResponse:
    """Read-only link to a ticket attachment (for example the diagnostics chart), so it can be shown in chat."""
    found = service().get_file(req.route_params.get("ticket_id"), req.route_params.get("filename"))
    if not found:
        return func.HttpResponse("File not found.", status_code=404)
    data, content_type = found
    if content_type == "text/markdown":
        import html

        import markdown

        text = data.decode("utf-8", errors="replace")
        banner = '<p style="background:#fff4ce;border:1px solid #f2c94c;padding:8px 12px;border-radius:6px"><b>Draft, pending human review.</b> Written by the knowledge agent from ticket findings. Not published to the knowledge base.</p>'
        title = html.escape(text.splitlines()[0].lstrip("# ").strip() if text.strip() else "Draft")
        page = KB_PAGE.format(title=title, body=banner + markdown.markdown(text, extensions=["tables", "fenced_code"]))
        return func.HttpResponse(page, mimetype="text/html", headers={"Cache-Control": "no-store"})
    return func.HttpResponse(data, mimetype=content_type, headers={"Cache-Control": "no-store"})


KB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "kb")
KB_PAGE = """<!doctype html><html lang="en"><head><meta charset="utf-8"><title>{title}</title>
<meta name="viewport" content="width=device-width, initial-scale=1">
<style>
body{{font-family:'Segoe UI',system-ui,sans-serif;margin:0;background:#f5f6f8;color:#1b1b1b}}
header{{background:#0f6cbd;color:#fff;padding:14px 32px;font-weight:600}}
header a{{color:#fff;text-decoration:none}}
main{{max-width:860px;margin:24px auto;background:#fff;padding:24px 40px;border-radius:8px;box-shadow:0 1px 4px rgba(0,0,0,.08)}}
code{{background:#f0f0f0;padding:1px 5px;border-radius:4px}} pre{{background:#f0f0f0;padding:12px;overflow:auto}}
table{{border-collapse:collapse}} td,th{{border:1px solid #ddd;padding:6px 10px}}
</style></head><body><header><a href="/api/kb">Contoso OrderSync Knowledge Base</a></header><main>{body}</main></body></html>"""


def _kb_files() -> dict:
    """KB-104 -> KB-104-http-503-connection-pool-exhausted-v4-2.md (same articles indexed by Foundry IQ)."""
    if not os.path.isdir(KB_DIR):
        return {}
    return {f.split("-")[0] + "-" + f.split("-")[1]: f for f in sorted(os.listdir(KB_DIR)) if f.upper().startswith("KB-") and f.endswith(".md")}


@app.route(route="kb/{kb_id?}", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def kb_article(req: func.HttpRequest) -> func.HttpResponse:
    """Public, read-only KB pages so agent citations can be clicked."""
    import html

    import markdown

    files = _kb_files()
    kb_id = (req.route_params.get("kb_id") or "").upper().removesuffix(".MD")
    kb_id = "-".join(kb_id.split("-")[:2]) if kb_id else ""
    if not kb_id:
        items = []
        for key, name in files.items():
            with open(os.path.join(KB_DIR, name), encoding="utf-8") as f:
                title = f.readline().lstrip("# ").strip()
            items.append(f'<li><a href="/api/kb/{key}">{html.escape(title)}</a></li>')
        page = KB_PAGE.format(title="Knowledge Base", body="<h1>Knowledge Base</h1><ul>" + "".join(items) + "</ul>")
        return func.HttpResponse(page, mimetype="text/html")
    if kb_id not in files:
        return func.HttpResponse(KB_PAGE.format(title="Not found", body="<h1>Article not found</h1>"), status_code=404, mimetype="text/html")
    with open(os.path.join(KB_DIR, files[kb_id]), encoding="utf-8") as f:
        text = f.read()
    title = html.escape(text.splitlines()[0].lstrip("# ").strip())
    body = markdown.markdown(text, extensions=["tables", "fenced_code"])
    return func.HttpResponse(KB_PAGE.format(title=title, body=body), mimetype="text/html")


@app.route(route="demo/reset", methods=["POST"])
def reset_demo(req: func.HttpRequest) -> func.HttpResponse:
    """Reset all demo data (requires a function key). Not exposed as an MCP tool, so agents can't call it.

    Like a real queue, every open ticket that arrives as New is handed to the Case Manager, so nothing sits
    unhandled. Customer memory is wiped and reseeded first, so the runs read only the seed facts. Agents only
    read memory, so nothing needs cleaning up afterwards. Add ?run=false to reset without starting any runs.
    """
    try:
        result = service().reset_demo()
        queue = []
        if req.params.get("run", "true").lower() != "false":
            queue = sorted(t["ticket_id"] for t in service().console_list()["tickets"] if t["status"] == "New")
            result["case_manager_started"] = queue
        try:
            result["memory"] = _reseed_memory()
        except Exception as e:
            logging.exception("Reset: memory reseed failed")
            result["memory"] = f"Customer memory reseed failed: {e}"
        for i, ticket_id in enumerate(queue):
            _dispatch(ticket_id, f"New ticket {ticket_id}", delay=i * 5)
        return func.HttpResponse(to_json(result), mimetype="application/json")
    except Exception as e:
        logging.exception("Reset failed")
        return func.HttpResponse(to_json({"error": str(e)}), status_code=500, mimetype="application/json")


MEMORY_STORE = "customer-memory"
MEMORY_PREVIEW = "MemoryStores=V1Preview"
LEGACY_MEMORY_SCOPES = ["contoso-support-customers"]  # the shared scope used before per-customer scopes


def _foundry(method: str, path: str, body: dict | None = None, preview: str | None = None, timeout: int = 60) -> dict:
    import urllib.request

    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    token = _credential().get_token("https://ai.azure.com/.default").token
    headers = {"Authorization": "Bearer " + token, "Content-Type": "application/json"}
    if preview:
        headers["Foundry-Features"] = preview
    data = json.dumps(body).encode() if body is not None else None
    with urllib.request.urlopen(urllib.request.Request(f"{endpoint}{path}", data=data, headers=headers, method=method), timeout=timeout) as r:
        return json.loads(r.read() or b"{}")


def _memory(method: str, path: str, body: dict | None = None) -> dict:
    sep = "&" if "?" in path else "?"
    return _foundry(method, f"/memory_stores/{MEMORY_STORE}{path}{sep}api-version=v1", body, MEMORY_PREVIEW)


def _memory_items(scope: str) -> list:
    """Every item in a customer's scope, oldest first. Listing is exact: no search model involved."""
    items, after = [], None
    while True:
        page = _memory("POST", "/items:list" + (f"?after={after}" if after else ""), {"scope": scope})
        items += page.get("data", [])
        if not page.get("has_more"):
            break
        after = page["last_id"]
    return sorted(items, key=lambda i: i.get("updated_at", 0))


def _reseed_memory() -> str:
    """Wipe every customer scope and write the seed items directly. Deterministic and takes a few seconds."""
    import seed_data

    for scope in [*seed_data.CUSTOMERS, *LEGACY_MEMORY_SCOPES]:
        _memory("POST", ":delete_scope", {"scope": scope})
    count = 0
    for scope, items in seed_data.MEMORY_SEED.items():
        for kind, content in items:
            _memory("POST", "/items", {"scope": scope, "kind": kind, "content": content})
            count += 1
    return f"Customer memory wiped and reseeded ({count} items)."


def _record_case_history(ticket_id: str, event: str) -> None:
    """Write (or replace) this ticket's case-history item in its customer's memory scope. Best effort: the
    ticket action already succeeded, so a memory failure is logged, not returned to the agent."""
    try:
        scope, tid, content = service().case_history(ticket_id, event)
        existing = next((i for i in _memory_items(scope) if i.get("content", "").startswith(f"{tid} (")), None)
        body = {"scope": scope, "kind": "user_profile", "content": content}  # user_profile so the portal Memories page lists it
        _memory("POST", f"/items/{existing['memory_id']}" if existing else "/items", body)
    except Exception:
        logging.exception("Case history for %s was not saved to customer memory", ticket_id)


# ---------- Support console: the "existing ticket system" UI that support engineers use ----------
# New tickets and approval decisions are handed to Foundry through a routine (case-intake), so every
# Case Manager run appears in the routine's run history in the Foundry portal, linked to its trace.
CONSOLE_HTML = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console.html")


def _engineer() -> str:
    """The demo has no sign-in, so the console acts as one support engineer (app setting SUPPORT_ENGINEER_NAME)."""
    return os.getenv("SUPPORT_ENGINEER_NAME", "").strip() or "Alex Rivera"


def _json(body, status=200):
    return func.HttpResponse(to_json(body), status_code=status, mimetype="application/json", headers={"Cache-Control": "no-store"})


def _routine() -> str:
    return os.getenv("CASE_INTAKE_ROUTINE", "case-intake")


def _send_routine(message: str) -> dict:
    """Fire the Foundry routine that runs the Case Manager. Foundry sometimes times out (408) or throttles, so retry.

    A duplicate delivery is harmless: each request carries a run token and the Case Manager runs a token once.
    """
    import urllib.error
    import urllib.request

    endpoint = os.getenv("FOUNDRY_PROJECT_ENDPOINT", "").rstrip("/")
    if not endpoint:
        raise RuntimeError("FOUNDRY_PROJECT_ENDPOINT is not set on the Function App.")
    attempts = 4
    for attempt in range(1, attempts + 1):
        token = _credential().get_token("https://ai.azure.com/.default").token
        request = urllib.request.Request(
            f"{endpoint}/routines/{_routine()}:dispatch_async?api-version=v1",
            data=json.dumps({"payload": {"type": "invoke_agent_responses_api", "input": message}}).encode(),
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=30) as response:
                return json.loads(response.read() or b"{}")
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", errors="replace")[:300]
            if e.code not in (408, 429, 500, 502, 503, 504) or attempt == attempts:
                raise RuntimeError(f"Foundry returned {e.code} for routine '{_routine()}': {detail}") from e
            logging.warning("Routine dispatch attempt %d returned %d; retrying", attempt, e.code)
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            if attempt == attempts:
                raise RuntimeError(f"Could not reach Foundry routine '{_routine()}': {e}") from e
            logging.warning("Routine dispatch attempt %d failed (%s); retrying", attempt, e)
        time.sleep(3 * attempt)


def _deliver(ticket_id: str, message: str, delay: float = 0) -> None:
    """Send a recorded run request to Foundry. If Foundry won't take it, mark the run stopped so the watchdog restarts it."""
    if delay:
        time.sleep(delay)
    try:
        result = _send_routine(message)
        logging.info("Dispatched %r for %s (dispatch %s)", message, ticket_id, result.get("dispatch_id"))
    except Exception as e:
        logging.exception("Could not dispatch %r for %s", message, ticket_id)
        found = RUN_TOKEN.search(message)
        try:
            service().record_stopped(ticket_id, found.group(1) if found else "", f"Foundry didn't accept the run request. {str(e)[:200]}")
        except Exception:
            logging.exception("Could not record the failed dispatch on %s", ticket_id)


_pool = ThreadPoolExecutor(max_workers=8, thread_name_prefix="dispatch")


def _dispatch(ticket_id: str, message: str, who: str = "ticket-system", delay: float = 0) -> dict:
    """Record the run request on the ticket right away, then send it to Foundry in the background.

    The caller (the support console) gets an answer immediately, even when Foundry is slow to accept the run.
    """
    message = with_token(message, new_run_token())
    service().record_dispatch(ticket_id, message, _routine(), who=who)
    _pool.submit(_deliver, ticket_id, message, delay)
    return {"routine": _routine(), "message": message}


def _console_call(fn, status=200):
    try:
        return _json(fn(), status)
    except ValueError as e:
        return _json({"error": str(e)}, 400)
    except RuntimeError as e:
        logging.exception("Console dispatch failed")
        return _json({"error": str(e)}, 502)
    except Exception:
        logging.exception("Console call failed")
        return _json({"error": "The ticket system hit an unexpected error. Check the Function App logs."}, 500)


@app.route(route="console", methods=["GET"])
def console_page(req: func.HttpRequest) -> func.HttpResponse:
    with open(CONSOLE_HTML, encoding="utf-8") as f:
        return func.HttpResponse(f.read(), mimetype="text/html", headers={"Cache-Control": "no-store"})


@app.route(route="console/examples", methods=["GET"])
def console_examples(req: func.HttpRequest) -> func.HttpResponse:
    from seed_data import CUSTOMERS, EXAMPLES

    return _json({
        "examples": [{"id": k, "label": v["label"]} for k, v in EXAMPLES.items()],
        "customers": [{"id": k, "name": v["name"], "tier": v["tier"]} for k, v in CUSTOMERS.items()],
    })


@app.route(route="console/tickets", methods=["GET", "POST"])
def console_tickets(req: func.HttpRequest) -> func.HttpResponse:
    if req.method == "GET":
        return _console_call(lambda: {**service().console_list(), "engineer": _engineer()})

    def submit():
        body = req.get_json() if req.get_body() else {}
        if body.get("example"):
            ticket = service().create_from_example(body["example"])
        else:
            ticket = service().create_ticket(
                body.get("customer_id"), body.get("title"), body.get("description"),
                body.get("product_version", ""), bool(body.get("log_profile")), body.get("log_profile") or None,
            )
        # A new case starts the Case Manager right away, with no human in the loop until the draft is ready.
        try:
            ticket["dispatch"] = _dispatch(ticket["ticket_id"], f"New ticket {ticket['ticket_id']}")
        except Exception as e:
            logging.exception("Could not start the Case Manager for %s", ticket["ticket_id"])
            ticket["dispatch_error"] = str(e)
        return ticket

    return _console_call(submit, 201)


@app.route(route="console/tickets/{ticket_id}", methods=["GET"])
def console_ticket(req: func.HttpRequest) -> func.HttpResponse:
    return _console_call(lambda: service().console_ticket(req.route_params.get("ticket_id")))


@app.route(route="console/tickets/{ticket_id}/run", methods=["POST"])
def console_run(req: func.HttpRequest) -> func.HttpResponse:
    """Send an existing ticket to the Case Manager (seed tickets), or with {"retry": true} rerun a run that failed."""
    def run():
        ticket_id = service().get_ticket(req.route_params.get("ticket_id"))["ticket_id"]
        body = req.get_json() if req.get_body() else {}
        message = service().retry_message(ticket_id) if body.get("retry") else f"New ticket {ticket_id}"
        return _dispatch(ticket_id, message, _engineer())

    return _console_call(run, 202)


@app.route(route="console/tickets/{ticket_id}/decision", methods=["POST"])
def console_decision(req: func.HttpRequest) -> func.HttpResponse:
    """The engineer's decision goes back through the Case Manager's own approval step.

    approve (optionally with an edited email), deny, or revise (with guidance for the drafting agent).
    """
    def decide():
        body = req.get_json() if req.get_body() else {}
        decision = body.get("decision")
        if decision not in ("approve", "deny", "revise"):
            raise ValueError("decision must be 'approve', 'deny', or 'revise'.")
        ticket = service().console_ticket(req.route_params.get("ticket_id"))
        tid = ticket["ticket_id"]
        if not ticket["awaiting_approval"]:
            raise ValueError(f"{tid} has no draft waiting for approval.")
        if decision == "revise":
            service().request_changes(tid, body.get("guidance"), _engineer())
        elif decision == "approve" and body.get("message") is not None:
            shown = (ticket["draft"] or "").replace("[Engineer name]", _engineer()).strip()
            if body["message"].strip() != shown:
                service().save_engineer_edit(tid, body["message"], _engineer())
        return _dispatch(tid, f"{decision} {tid} by {_engineer()}", _engineer())

    return _console_call(decide, 202)


@app.timer_trigger(schedule="*/20 * * * * *", arg_name="timer", run_on_startup=False, use_monitor=False)
def run_watchdog(timer: func.TimerRequest) -> None:
    """Every 20 seconds: restart Case Manager runs that Foundry dropped (408s), or mark them failed after two restarts."""
    for ticket_id, action in service().watchdog_actions():
        try:
            if action[0] == "restart":
                _, reason, message = action
                logging.warning("Restarting the Case Manager run for %s: %s", ticket_id, reason)
                service().record_restart(ticket_id, message, _routine(), reason)
                _deliver(ticket_id, message)
            else:
                logging.error("Case Manager run for %s failed: %s", ticket_id, action[1])
                service().record_failed(ticket_id, action[1])
        except Exception:
            logging.exception("Watchdog could not handle %s", ticket_id)
