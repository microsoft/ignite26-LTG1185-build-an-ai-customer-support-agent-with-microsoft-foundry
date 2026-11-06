"""Case Manager: an Agent Framework workflow, hosted on Microsoft Foundry.

Turn 1, "New ticket TCK-1042". The route is decided in code from facts, not by a model:
  logs attached, KB match:     intake -> triage -> diagnostics -> drafting (fix options) -> approval
  no logs:                     intake -> triage -> drafting (ask for what's missing, link KB-109) -> approval
  logs, no KB or low confidence: intake -> triage -> diagnostics -> knowledge-agent (draft KB, escalate)
                                 -> drafting (escalation update) -> approval
Turn 2, "approve" or "deny":
  intake -> decision: send the saved draft and set "Awaiting customer" (or keep "Escalated"), or keep it and note the decision
  (if the engineer edited the draft in the support console, the edited draft is the one sent)
Turn 2, "revise" (the engineer's guidance is saved on the ticket as a "[Changes requested]" note):
  intake -> revise -> drafting (rewrites the draft with the guidance) -> approval

The specialists are Foundry prompt agents that are called by name. The orchestration is
plain code, so the order of steps and the approval gate are fixed and don't depend on a model.
Nothing customer-facing is sent until a support engineer replies "approve".
"""

import asyncio
import base64
import json
import logging
import os
import re
import time
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import httpx
from agent_framework import Executor, InMemoryCheckpointStorage, Message, WorkflowBuilder, WorkflowContext, handler
from agent_framework.foundry import FoundryAgent, FoundryToolbox, ResponsesHostServer
from agent_framework_foundry_hosting._state_store import ContextScopedStoreProvider
from azure.identity import DefaultAzureCredential
from dotenv import load_dotenv
from typing_extensions import Never

load_dotenv()
logger = logging.getLogger("case-manager")

PROJECT_ENDPOINT = os.environ["FOUNDRY_PROJECT_ENDPOINT"]
TOOLBOX_ENDPOINT = os.environ["TOOLBOX_ENDPOINT"]
KB_BASE_URL = os.environ.get("KB_BASE_URL", "").rstrip("/")
# Your system's ticket ID format. Change this to match yours, for example r"\bINC\d{7}\b" for ServiceNow.
TICKET_ID = re.compile(r"\bTCK-\d+\b", re.IGNORECASE)
# Only a message that asks for the queue picks a ticket on its own; any other message must name a ticket.
QUEUE_PICKUP = re.compile(r"\boldest\b", re.IGNORECASE)
APPROVE = re.compile(r"^\s*(approve|approved|yes|send it|send)\b", re.IGNORECASE)
DENY = re.compile(r"^\s*(deny|denied|reject|no|don't send|do not send)\b", re.IGNORECASE)
REVISE = re.compile(r"^\s*(revise|request changes)\b", re.IGNORECASE)
DRAFT_MARKER = "[Draft awaiting approval]"
CHANGES_MARKER = "[Changes requested]"
# Run tracking, shared with the ticket system (src/ticket-system/tickets_core.py). Each request carries a
# token, e.g. "New ticket TCK-1042 (run 3f9a2c)". The ticket system's watchdog restarts runs that stop.
REQUEST_MARKERS = ("[Sent to Case Manager]", "[Case Manager restarted]")
STARTED_MARKER = "[Case Manager started]"
STOPPED_MARKER = "[Run stopped]"
RUN_TOKEN = re.compile(r"\s*\(run ([a-z0-9]+)\)", re.IGNORECASE)
TRANSIENT = re.compile(
    r"\b(?:408|429|500|502|503|504)\b|RequestTimeout|timed? ?out|timeout|ServiceUnavailable|server_error"
    r"|APIConnectionError|ConnectError|ReadError|RemoteProtocolError|Connection (?:reset|aborted|closed)",
    re.IGNORECASE,
)
STEP_ATTEMPTS = 3
TOOLBOX_ATTEMPTS = 5  # Foundry 408s come in bursts of ~20-60s; 5 tries with growing waits rides one out
AGENT_CALL_TIMEOUT = 180  # seconds; a hung agent call becomes a retry instead of a silent stall
TOOLBOX_CALL_TIMEOUT = 60  # seconds; after a 408 the MCP response can be lost, and the call would wait forever
REPORT_MARKER = "[Report: "  # "[Report: Triage]" - the support console shows these as formatted cards
SEND_TOOL = "ticket-system___send_customer_update"
STATUS_TOOL = "ticket-system___update_ticket_status"
NOTE_TOOL = "ticket-system___add_ticket_note"
GET_TOOL = "ticket-system___get_ticket"
ATTACH_TOOL = "ticket-system___attach_file"
LIST_TOOL = "ticket-system___list_open_tickets"
LAST_TICKET = "last_ticket_id"
CITATION_MARK = re.compile(r"\u3010[^\u3011]*\u3011")  # 【7:0†source】
SANDBOX_LINK = re.compile(r"!?\[([^\]]*)\]\((?!https?://)[^)]*\)")  # sandbox:/mnt/data/x.png, /mnt/data/x.png, x.png
KB_ID = re.compile(r"KB-\d+", re.IGNORECASE)  # your knowledge article ID format
CUSTOMER_KBS = {"KB-109"}  # written for customers, so they may appear in the email body; list your own
IMAGE_EXT = (".png", ".jpg", ".jpeg")

credential = DefaultAzureCredential()


# Messages between steps are plain dicts (not custom classes) so the workflow's checkpoints
# stay serializable.
def case_state(ticket_id: str, run: str = "") -> dict:
    return {"kind": "case", "ticket_id": ticket_id, "run": run, "results": {}}


def decision_msg(ticket_id: str, approved: bool, engineer: str = "", run: str = "") -> dict:
    return {"kind": "decision", "ticket_id": ticket_id, "approved": approved, "engineer": engineer, "run": run}


def sign(message: str, engineer: str) -> str:
    """Put the approving engineer's name on the email; without one, sign as the team."""
    if engineer:
        return message.replace("[Engineer name]", engineer)
    return re.sub(r"\[Engineer name\],?\s*", "", message)


def approver(text: str) -> str:
    """The support console sends "approve TCK-1045 by Alex Rivera"; a chat "approve" has no name."""
    found = re.search(r"\bby\s+([A-Za-z][A-Za-z .'-]{1,60}?)\s*$", text)
    return found.group(1) if found else ""


_latest_versions: dict[str, tuple[float, str]] = {}
LATEST_VERSION_TTL = 300  # seconds; a newly published version is picked up within five minutes


def latest_version(name: str) -> str:
    """The newest published version of a prompt agent, cached briefly so each step doesn't pay for a lookup."""
    cached = _latest_versions.get(name)
    if cached and time.monotonic() - cached[0] < LATEST_VERSION_TTL:
        return cached[1]
    token = credential.get_token("https://ai.azure.com/.default").token
    r = httpx.get(f"{PROJECT_ENDPOINT}/agents/{name}?api-version=v1", headers={"Authorization": "Bearer " + token}, timeout=15)
    r.raise_for_status()
    version = str(r.json()["versions"]["latest"]["version"])
    _latest_versions[name] = (time.monotonic(), version)
    return version


def specialist(name: str) -> FoundryAgent:
    """Reference an existing Foundry prompt agent by name and version.

    Pin a version with <NAME>_VERSION (for example TRIAGE_AGENT_VERSION=9) so a newly published prompt can't
    change production behavior until you choose to roll it out. Without a pin, the latest version is used.
    The version must always be passed explicitly: without one, FoundryAgent falls back to FOUNDRY_AGENT_VERSION,
    which the hosting platform sets to the Case Manager's own version.
    """
    setting = f"{name.upper().replace('-', '_')}_VERSION"
    version = os.environ.get(setting, "").strip() or latest_version(name)
    return FoundryAgent(project_endpoint=PROJECT_ENDPOINT, agent_name=name, agent_version=version, credential=credential)


def no_dashes(text: str) -> str:
    """No em or en dashes in anything we show: ranges become "to", asides become commas."""
    text = re.sub(r"(\d)\s*[\u2013\u2014]\s*(\d)", r"\1 to \2", text)
    text = re.sub(r"\s*[\u2014\u2013]\s*(?=\w)", ", ", text)
    return text.replace("\u2014", "-").replace("\u2013", "-")


def draft_body(draft: str) -> str:
    """Pull the email body out of the drafting agent's reply."""
    match = re.search(r"\*\*Body:\*\*\s*(.+?)\s*\*\*Why this draft", draft, re.DOTALL)
    return (match.group(1) if match else draft).strip()


def email_end(draft: str) -> int:
    """Where the customer email ends and the notes for the approver ("Why this draft") begin."""
    match = re.search(r"\*\*Why this draft", draft)
    return match.start() if match else len(draft)


KB_REF = r"(?:our\s+)?(?:article\s+|knowledge base\s+)?KB-\d+(?:\s*(?:,|and)\s*KB-\d+)*"


def tidy_draft(draft: str, sources: list[str] | None = None, due: str | None = None) -> str:
    """Policy in code, not just in the prompt: KB articles are internal, so none in the email body.

    The approver's "Why this draft" line keeps them, as clickable links. `sources` are the KB ids
    the agent actually retrieved, used when the model cites raw markers like "30:3" instead of names.
    """
    cut = email_end(draft)
    email, notes = draft[:cut], draft[cut:]

    def internal_only(m: re.Match) -> str:
        ids = {k.upper() for k in KB_ID.findall(m.group(0))}
        return m.group(0) if ids and ids <= CUSTOMER_KBS else ""

    email = re.sub(rf"\s*\((?:see|per|ref(?:erence)?:?)?\s*{KB_REF}\)", internal_only, email, flags=re.IGNORECASE)
    email = re.sub(rf",?\s*\b(?:per|see|as described in|according to)\s+{KB_REF}", internal_only, email, flags=re.IGNORECASE)
    if KB_BASE_URL:
        email = re.sub(r"(?<![\[/])\b(KB-109)\b(?!\])", rf"[\1]({KB_BASE_URL}/\1)", email)
    if {k.upper() for k in KB_ID.findall(email)} - CUSTOMER_KBS:
        logger.warning("Draft body still mentions an internal KB article after cleanup")
    # Fix options read as sub-points of "Fix options:", so indent "- Option A ..." bullets one level.
    email = re.sub(r"(?m)^[-*]\s+(?=(?:\*\*)?Option\s+[A-Z0-9]\b)", "  - ", email)
    notes = re.sub(r"\s*\((?:citations?|sources?)\b(?![^)]*KB-\d)[^)]*\)", "", notes, flags=re.IGNORECASE)
    notes = re.sub(r"\s*\([^)]*\b\d{1,3}:\d{1,3}\b[^)]*\)", "", notes)  # "(KB retrieval citations 10:0 and 10:2)"
    notes = re.sub(  # "; internal tokens preserved: 30:3, 30:0" or ". Preserved internal tokens 30:3 for review."
        r"[;,.]?\s*[^.;:*]*?\b(?:tokens?|citations?|markers?)\b[^.;]*?\b\d{1,3}:\d{1,3}\b[^.;]*", "", notes, flags=re.IGNORECASE
    )
    missing = [kb for kb in dict.fromkeys(sources or []) if kb not in notes]
    if missing and notes:
        line_end = notes.find("\n") if "\n" in notes else len(notes)
        notes = f"{notes[:line_end].rstrip()} Sources: {', '.join(missing)}.{notes[line_end:]}"
    if KB_BASE_URL:
        notes = re.sub(r"(?<![\[/])\b(KB-\d+)\b(?!\])", rf"[\1]({KB_BASE_URL}/\1)", notes)
    notes = dedupe_links(notes)
    return enforce_due(friendly_times(email), due) + notes


def dedupe_links(notes: str) -> str:
    """Keep the first link to each KB article; drop repeats and the empty brackets they leave."""
    seen: set[str] = set()

    def first(m: re.Match) -> str:
        kb = m.group(1).upper()
        if kb in seen:
            return ""
        seen.add(kb)
        return m.group(0)

    notes = re.sub(r"\[(KB-\d+)\]\([^)]*\)", first, notes)
    notes = re.sub(r"\s*\((?:[\s,;]|and)*\)", "", notes)
    notes = re.sub(r"(?:\s*,)+(\s*[.;])", r"\1", notes)
    notes = re.sub(r"(?:\s*,){2,}", ",", notes)
    return re.sub(r"[ \t]{2,}", " ", re.sub(r"\s+([.,;])", r"\1", notes))


EASTERN = ZoneInfo("America/New_York")


def plain_time(utc: datetime) -> str:
    """16:50 UTC -> '12:50 PM ET (16:50 UTC)'."""
    et = utc.astimezone(EASTERN)
    return f"{et.strftime('%I:%M %p').lstrip('0')} ET ({utc.strftime('%H:%M')} UTC)"


def now_text() -> str:
    utc = datetime.now(timezone.utc)
    return f"{plain_time(utc)} on {utc.astimezone(EASTERN).strftime('%A, %B %d, %Y')}"


# KB-107 first-response targets: (severity, premier) -> minutes on the clock, or a business-hours target.
RESPONSE_MINUTES = {("Sev1", True): 15, ("Sev1", False): 60, ("Sev2", True): 60}
BUSINESS_TARGET = {
    ("Sev2", False): "4 business hours", ("Sev3", True): "4 business hours", ("Sev3", False): "1 business day",
    ("Sev4", True): "1 business day", ("Sev4", False): "2 business days",
}
FRIENDLY_TIME = r"\d{1,2}(?::\d{2})?\s*[AaPp]\.?[Mm]\.?\s*(?:ET|EDT|EST|Eastern)(?:\s*\(\d{2}:\d{2} UTC\))?"
UPDATE_LINE = re.compile(r"\b(?:update|follow(?:-| )?up|get back|hear from us|check in)\b", re.IGNORECASE)


def update_due(triage: str) -> tuple[str, str | None]:
    """The next-update promise comes from code: now + the KB-107 target for triage's severity and the tier."""
    found = re.search(r"\bSev\s*([1-4])\b", triage)
    severity = f"Sev{found.group(1)}" if found else "Sev2"
    premier = "premier" in triage.lower()
    tier = "Premier" if premier else "Standard"
    minutes = RESPONSE_MINUTES.get((severity, premier))
    if minutes:
        due = plain_time((datetime.now(timezone.utc) + timedelta(minutes=minutes)).replace(second=0, microsecond=0))
        return f"Promise the next update by {due} (KB-107 {severity} {tier} target). Write the time as given, without words like \"exactly\".", due
    target = BUSINESS_TARGET.get((severity, premier), "1 business day")
    return f"Promise the next update within {target} (KB-107 {severity} {tier} target). Do not give a clock time.", None


def enforce_due(email: str, due: str | None) -> str:
    """If the model wrote a different next-update time, replace it with the computed one."""
    if not due:
        return email
    return "\n".join(
        re.sub(FRIENDLY_TIME, due, line, count=1) if UPDATE_LINE.search(line) else line
        for line in email.split("\n")
    )


def friendly_times(email: str) -> str:
    """Customers get Eastern time with UTC in parentheses. Rewrite ISO stamps and UTC-only times."""
    today = datetime.now(timezone.utc)

    def at(hour: int, minute: int) -> str:
        return plain_time(today.replace(hour=hour % 24, minute=minute, second=0, microsecond=0))

    def iso(m: re.Match) -> str:
        return plain_time(datetime.fromisoformat(m.group(0).replace("Z", "+00:00")))

    def twelve_hour(m: re.Match) -> str:
        hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == "p" else 0)
        return at(hour, int(m.group(2) or 0))

    email = re.sub(r"\b\d{4}-\d{2}-\d{2}T\d{2}:\d{2}(?::\d{2})?Z\b", iso, email)
    email = re.sub(r"(?<![(\d:])\b(\d{1,2})(?::(\d{2}))?\s*([AaPp])\.?[Mm]\.?\s*UTC\b", twelve_hour, email)
    return re.sub(r"(?<![(\d:])\b(\d{1,2}):(\d{2})\s*UTC\b", lambda m: at(int(m.group(1)), int(m.group(2))), email)


_toolbox: FoundryToolbox | None = None
_toolbox_lock = asyncio.Lock()


async def _open_toolbox() -> FoundryToolbox:
    """One toolbox session for the process: connecting and listing tools costs ~6s, so do it once, not per call."""
    global _toolbox
    async with _toolbox_lock:
        if _toolbox is None:
            toolbox = FoundryToolbox(credential, url=TOOLBOX_ENDPOINT, name="support-toolbox", request_timeout=TOOLBOX_CALL_TIMEOUT)
            await toolbox.__aenter__()
            _toolbox = toolbox
        return _toolbox


async def _reset_toolbox(stale: FoundryToolbox) -> None:
    global _toolbox
    async with _toolbox_lock:
        if _toolbox is stale:
            _toolbox = None
            try:
                await stale.__aexit__(None, None, None)
            except Exception:  # noqa: BLE001 - the session is already broken; just drop it
                pass


async def call_toolbox(tool: str, arguments: dict) -> str:
    """Call one ticket-system tool through the Foundry toolbox (one governed endpoint for all tools)."""
    attempts = TOOLBOX_ATTEMPTS
    for attempt in range(1, attempts + 1):
        # Connecting can time out too (Foundry 408 on the MCP handshake). Nothing was sent yet, so always retry it.
        try:
            toolbox = await _open_toolbox()
        except Exception:
            if attempt == attempts:
                raise
            logger.warning("Toolbox connect failed for %s (attempt %d); retrying", tool, attempt, exc_info=True)
            await asyncio.sleep(3 * attempt)
            continue
        try:
            call_tool = next(f for f in toolbox.functions if f.name == "call_tool")
            result = await call_tool.invoke(arguments={"name": tool, "arguments": arguments})
            break
        except Exception:
            # An expired or dropped session, or a Foundry timeout: reconnect and try again, then let the error surface.
            await _reset_toolbox(toolbox)
            if attempt == attempts or tool == SEND_TOOL:  # never risk emailing the customer twice
                raise
            logger.warning("Toolbox call %s failed (attempt %d); retrying", tool, attempt, exc_info=True)
            await asyncio.sleep(3 * attempt)
    if isinstance(result, list):
        return " ".join((getattr(c, "text", None) or "") for c in result)
    return str(result)


def is_transient(exc: BaseException) -> bool:
    """Foundry timeouts (408), throttling, and dropped connections are worth another try. A Foundry IQ timeout
    arrives wrapped in a 400 tool error, so check the whole exception chain's text, not just the type."""
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        if TRANSIENT.search(f"{type(exc).__name__} {exc}"):
            return True
        exc = exc.__cause__ or exc.__context__
    return False


async def add_note(ticket_id: str, note: str) -> None:
    """Best effort: a progress note must never be the thing that breaks a run."""
    try:
        await call_toolbox(NOTE_TOOL, {"ticket_id": ticket_id, "author": "case-manager", "note": note})
    except Exception:
        logger.exception("Could not add a note on %s", ticket_id)


async def stop_run(message: dict, step: str, exc: BaseException) -> None:
    """Tell the ticket system this run gave up, so its watchdog restarts it right away instead of waiting."""
    logger.error("%s failed for %s (run %s)", step, message.get("ticket_id"), message.get("run"), exc_info=exc)
    if message.get("ticket_id") and message.get("run"):
        why = "Foundry kept timing out." if is_transient(exc) else f"{type(exc).__name__}."
        await add_note(message["ticket_id"], f"{STOPPED_MARKER} run {message['run']}: {step} could not finish. {why}")


def latest_run(notes: list[dict]) -> str:
    """The token of the newest run requested on the ticket."""
    for note in reversed(notes):
        text = note.get("text", "")
        if text.startswith(REQUEST_MARKERS):
            found = RUN_TOKEN.search(text)
            return found.group(1).lower() if found else ""
    return ""


async def still_current(ticket_id: str, run: str) -> bool:
    """False if a newer run was requested for this ticket (a restart), so this one should not write results."""
    if not run:
        return True
    ticket = parse_json(await call_toolbox(GET_TOOL, {"ticket_id": ticket_id}))
    return latest_run(ticket.get("notes", [])) in ("", run)


async def claim(ticket_id: str, run: str) -> bool:
    """Mark the run started. False if it's a duplicate delivery of the same run, or a newer run replaced it."""
    instance = os.urandom(3).hex()
    await call_toolbox(NOTE_TOOL, {"ticket_id": ticket_id, "author": "case-manager", "note": f"{STARTED_MARKER} run {run} (instance {instance})"})
    notes = parse_json(await call_toolbox(GET_TOOL, {"ticket_id": ticket_id})).get("notes", [])
    if latest_run(notes) not in ("", run):
        logger.warning("Run %s on %s was replaced by a newer run; stopping", run, ticket_id)
        return False
    first = next((n.get("text", "") for n in notes if n.get("text", "").startswith(f"{STARTED_MARKER} run {run}")), "")
    if f"(instance {instance})" not in first:
        logger.warning("Run %s on %s is already being handled; skipping the duplicate", run, ticket_id)
        return False
    return True


def parse_json(text: str) -> dict:
    start, end = text.find("{"), text.rfind("}")
    return json.loads(text[start : end + 1]) if start >= 0 and end > start else {}


async def foundry_get(path: str) -> httpx.Response:
    """Short timeout with one retry: this call only enriches a result, so it must never stall a live run."""
    try:
        return await _foundry_get_once(path)
    except httpx.TimeoutException:
        return await _foundry_get_once(path)


async def _foundry_get_once(path: str) -> httpx.Response:
    """GET from the Foundry project's OpenAI-compatible API with the agent's own identity."""
    token = await asyncio.to_thread(credential.get_token, "https://ai.azure.com/.default")
    async with httpx.AsyncClient(timeout=10) as http:
        r = await http.get(f"{PROJECT_ENDPOINT}/openai/v1/{path}", headers={"Authorization": f"Bearer {token.token}"})
        r.raise_for_status()
        return r


def link_citations(text: str, annotations: list[dict], internal_from: int = 0) -> str:
    """Turn Foundry IQ markers like 【7:0†source】 into clickable [KB-104](...) links.

    Markers before `internal_from` (a customer email body) are dropped: KB articles are internal.
    """
    urls = [a for a in sorted(annotations, key=lambda a: a.get("start_index", 0)) if a.get("type") == "url_citation"]
    links = iter(urls)

    def replace(m: re.Match) -> str:
        a = next(links, None)
        kb = KB_ID.search((a or {}).get("url", "") + " " + (a or {}).get("title", "")) if a else None
        if not kb or (m.start() < internal_from and kb.group(0).upper() not in CUSTOMER_KBS):
            return ""
        kb_id = kb.group(0).upper()
        return f" [{kb_id}]({KB_BASE_URL}/{kb_id})" if KB_BASE_URL else f" ({kb_id})"

    text = CITATION_MARK.sub(replace, text)
    text = re.sub(r"\b(KB-\d+)\s*\[\1\]\(([^)]*)\)", r"[\1](\2)", text)  # "per KB-104 [KB-104](..)" -> one link
    return re.sub(r"(\s*\[(KB-\d+)\]\([^)]*\))(\s*\[\2\]\([^)]*\))+", r"\1", text)  # same source twice in a row


async def attach_chart(ticket_id: str, annotations: list[dict]) -> str | None:
    """Copy the Code Interpreter chart from the agent's sandbox to the ticket. Returns a public link."""
    files = [a for a in annotations if a.get("type") == "container_file_citation" and a.get("filename", "").lower().endswith(IMAGE_EXT)]
    if not files:
        return None
    chart = next((a for a in reversed(files) if a.get("end_index", 0) > a.get("start_index", 0)), files[-1])
    content = (await foundry_get(f"containers/{chart['container_id']}/files/{chart['file_id']}/content")).content
    name = chart["filename"] if not chart["filename"].startswith("cfile_") else "errors_by_hour.png"
    result = parse_json(await call_toolbox(ATTACH_TOOL, {
        "ticket_id": ticket_id, "filename": name, "content_base64": base64.b64encode(content).decode(),
    }))
    return result.get("url")


async def presentable(response, ticket_id: str, customer_facing: bool = False, due: str | None = None) -> str:
    """Make a specialist's answer readable outside its own playground: clickable KB sources, visible chart."""
    text = response.text or ""
    sources: list[str] = []
    try:
        stored = (await foundry_get(f"responses/{response.response_id}")).json()
        parts = [c for o in stored.get("output", []) if o.get("type") == "message" for c in o.get("content", []) if c.get("type") == "output_text"]
        if parts:
            text = "\n\n".join(
                link_citations(c.get("text", ""), c.get("annotations") or [], email_end(c.get("text", "")) if customer_facing else 0)
                for c in parts
            )
        for a in (a for c in parts for a in (c.get("annotations") or []) if a.get("type") == "url_citation"):
            kb = KB_ID.search(a.get("url", "") + " " + a.get("title", ""))
            if kb:
                sources.append(kb.group(0).upper())
        chart_url = await attach_chart(ticket_id, [a for c in parts for a in (c.get("annotations") or [])])
        if chart_url:
            image = f"![Errors per hour]({chart_url})\n\n[Open the chart]({chart_url})"
            text = SANDBOX_LINK.sub(image, text, count=1) if SANDBOX_LINK.search(text) else f"{text}\n\n{image}"
    except Exception:
        logger.exception("Could not enrich the %s response; showing plain text", response.response_id)
    text = SANDBOX_LINK.sub("", CITATION_MARK.sub("", text))
    text = re.sub(r"(?:Chart:\s*)?(?:sandbox:)?/mnt/data/\S+", "", text)  # bare sandbox paths nobody can open
    text = re.sub(r"(?im)^\s*[\[(]?\s*(?:the\s+)?chart\b[^\n]*\b(?:above|below)\b[^\n]*$\n?", "", text)  # "(Chart shown above.)"
    return no_dashes(tidy_draft(text, sources, due)) if customer_facing else no_dashes(text)


class Intake(Executor):
    """Read the newest message: a new case ("New ticket TCK-1042"), a queue pickup with no ID
    ("Process the oldest New ticket"), or a decision ("approve")."""

    @handler
    async def start(self, messages: list[Message], ctx: WorkflowContext[dict, str]) -> None:
        user_texts = [m.text or "" for m in messages if str(m.role) == "user"] or [m.text or "" for m in messages]
        text = user_texts[-1] if user_texts else ""
        token = RUN_TOKEN.search(text)
        run = token.group(1).lower() if token else ""
        text = RUN_TOKEN.sub("", text).strip()
        found = TICKET_ID.search(text)
        try:
            await self.route(text, found, run, ctx)
        except Exception as e:
            await stop_run({"ticket_id": found.group(0).upper() if found else "", "run": run}, "Intake", e)
            raise

    async def route(self, text: str, found: re.Match | None, run: str, ctx: WorkflowContext[dict, str]) -> None:
        ticket_id = found.group(0).upper() if found else ctx.get_state(LAST_TICKET)
        if found and run and not await claim(ticket_id, run):
            await ctx.yield_output(f"Run {run} for {ticket_id} is already handled by another run. Nothing to do.")
            return

        if APPROVE.search(text) or DENY.search(text) or REVISE.search(text):
            if not ticket_id:
                await ctx.yield_output("Which ticket? For example: approve TCK-1042")
                return
            if REVISE.search(text):
                await ctx.send_message({"kind": "revise", "ticket_id": ticket_id, "engineer": approver(text), "run": run})
                return
            await ctx.send_message(decision_msg(ticket_id, bool(APPROVE.search(text)), approver(text), run))
            return

        if not found and not QUEUE_PICKUP.search(text):
            await ctx.yield_output(
                "I couldn't find a ticket ID in that message, so I didn't start a run. "
                "Name the ticket, for example: New ticket TCK-1042. "
                "If your ticket IDs look different, update TICKET_ID in main.py."
            )
            return
        if not found:
            # A queue pickup with no ticket named (the routine's "Process the oldest New ticket"): take the queue's oldest.
            listing = parse_json(await call_toolbox(LIST_TOOL, {"status": "New"}))
            if listing.get("error"):
                await ctx.yield_output(listing["error"])
                return
            queue = listing.get("tickets") or []
            if not queue:
                await ctx.yield_output("No tickets are waiting in New status. Nothing to do.")
                return
            ticket_id = queue[0]["ticket_id"].upper()
        ticket = parse_json(await call_toolbox(GET_TOOL, {"ticket_id": ticket_id}))
        if not ticket.get("ticket_id"):
            await ctx.yield_output(ticket.get("error") or f"I couldn't read {ticket_id} from the ticket system.")
            return
        ctx.set_state(LAST_TICKET, ticket_id)
        state = case_state(ticket_id, run)
        state["has_logs"] = bool(ticket.get("has_logs"))
        plan = "triage, diagnostics, and a customer draft" if state["has_logs"] else "triage and a request for the missing information (no logs are attached)"
        await ctx.yield_output(f"## Case Manager: {ticket_id}\nStarting {plan}.\n\n")
        await ctx.send_message(state)


class SpecialistStep(Executor):
    """Run one Foundry prompt agent and pass its result to the next step."""

    report = False  # True: also save the formatted result on the ticket
    def __init__(self, id: str, agent_name: str, title: str, prompt: str, customer_facing: bool = False):
        super().__init__(id=id)
        self.agent_name = agent_name
        self.title = title
        self.prompt = prompt
        self.customer_facing = customer_facing
        self._agent: FoundryAgent | None = None

    @handler
    async def run_step(self, state: dict, ctx: WorkflowContext[dict, str]) -> None:
        try:
            await self.step(state, ctx)
        except Exception as e:
            await stop_run(state, self.title, e)
            raise

    async def ask(self, prompt: str, ticket_id: str):
        """Call the Foundry agent. Foundry sometimes times out (408); try again rather than lose the whole run."""
        for attempt in range(1, STEP_ATTEMPTS + 1):
            try:
                return await asyncio.wait_for(self._agent.run(prompt), AGENT_CALL_TIMEOUT)
            except Exception as e:
                if attempt == STEP_ATTEMPTS or not is_transient(e):
                    raise
                logger.warning("%s attempt %d failed on %s; retrying", self.title, attempt, ticket_id, exc_info=True)
                await add_note(ticket_id, f"[Case Manager] Foundry timed out during {self.title}. Trying again ({attempt + 1} of {STEP_ATTEMPTS}).")
                await asyncio.sleep(3 * attempt)

    async def step(self, state: dict, ctx: WorkflowContext[dict, str]) -> None:
        self._agent = self._agent or specialist(self.agent_name)
        due_rule, due = update_due(state["results"].get("triage", "")) if self.customer_facing else ("", None)
        mode = state.get("mode", "fix")
        if self.customer_facing and mode == "info":
            due_rule, due = "", None
        prompt = self.prompt.format(
            ticket_id=state["ticket_id"], now=now_text(), due_rule=due_rule,
            facts=TRIAGE_FACTS if not state.get("has_logs", True) else "",
            mode_rule=MODE_RULES.get(mode, "").format(kb_base_url=KB_BASE_URL),
            diagnostics=state["results"].get("diagnostics", ""),
        )
        if self.customer_facing and state.get("revision"):
            prompt += "\n\n" + state["revision"]
        response = await self.ask(prompt, state["ticket_id"])
        text = await presentable(response, state["ticket_id"], self.customer_facing, due)
        state["results"][self.id] = text
        state["step"] = state.get("step", 0) + 1
        await ctx.yield_output(f"### {state['step']}. {self.title} ({self.agent_name})\n{self.display(text)}\n\n")
        if self.report:
            await self.save_report(state["ticket_id"], text)
        await self.after(state, text, ctx)
        await ctx.send_message(state)

    async def save_report(self, ticket_id: str, text: str) -> None:
        """Save the formatted result (KB links, chart) on the ticket so the support console shows what the chat shows."""
        try:
            await call_toolbox(NOTE_TOOL, {"ticket_id": ticket_id, "author": self.agent_name, "note": f"{REPORT_MARKER}{self.title}]\n{text}"})
        except Exception:
            logger.exception("Could not save the %s report on %s", self.id, ticket_id)

    def display(self, text: str) -> str:
        return text

    async def after(self, state: dict, text: str, ctx: WorkflowContext[dict, str]) -> None:
        """Hook for steps that decide the route or act on the result."""


TRIAGE_FACTS = (
    "Facts from the ticket system: no logs are attached. Do not guess a cause. "
    "In the summary, list what is missing to diagnose it (logs, product version, when it started, example order IDs). "
    "Next step: Request information from the customer."
)
MODE_RULES = {
    "info": (
        "There are no logs and no diagnosis yet. Do not guess a cause and do not suggest a fix. "
        "List only the missing information triage listed, one bulleted item each. "
        "Never mention formatting, bullets, or the customer's preferences in the email itself. "
        "KB-109 is a customer-facing guide, so it is the one article you may link in the body: "
        "[How to collect and send OrderSync diagnostic logs]({kb_base_url}/KB-109). "
        "Say we will start the analysis as soon as the information arrives. Do not promise a clock time."
    ),
    "escalate": (
        "No documented fix exists, so the case is escalated to an engineer. "
        "Describe only what the customer would recognize, such as which orders are affected and since when. "
        "Do not share a cause, and do not mention diagnostics, KB articles, logs, or attachments. "
        "Say an engineer is investigating. Do not suggest any fix, workaround, or config change. "
        "You may ask for at most one thing that would help the engineer, and say why it helps. "
        "Never say analysis will start later. Mention the escalation once."
    ),
    "fix": (
        "The root cause is identified and the KB article gives the fix. The customer applies the fix themselves. "
        "End with one clear next action for the customer, for example: apply Option A and reply to confirm the errors stopped. "
        "Never ask the customer to confirm whether we should apply a fix, and never say analysis will start later."
    ),
}


def needs_escalation(diagnostics: str) -> bool:
    """Code decides the route from the diagnostics result: no matching KB article, or low confidence."""
    known = re.search(r"Known issue:\**\s*(.*)", diagnostics)
    matched = bool(known and KB_ID.search(known.group(1)) and not re.search(r"no matching", known.group(1), re.IGNORECASE))
    low = re.search(r"Confidence:\**\s*Low", diagnostics, re.IGNORECASE)
    lowered = diagnostics.lower()
    return not matched or bool(low) or "needs engineer review" in lowered or "needs engineering review" in lowered


class TriageStep(SpecialistStep):
    report = True

    async def after(self, state: dict, text: str, ctx: WorkflowContext[dict, str]) -> None:
        if not state.get("has_logs", True):
            state["mode"] = "info"


class DiagnosticsStep(SpecialistStep):
    report = True

    async def after(self, state: dict, text: str, ctx: WorkflowContext[dict, str]) -> None:
        state["mode"] = "escalate" if needs_escalation(text) else "fix"


class KnowledgeStep(SpecialistStep):
    """No KB article covers this: escalate to an engineer, and save the knowledge agent's draft article for review."""

    def display(self, text: str) -> str:
        if re.match(r"\s*Existing article:", text, re.IGNORECASE):
            return text
        title = next((l.lstrip("# ").strip() for l in text.splitlines() if l.strip()), "Draft article")
        sections = re.findall(r"^##\s+(.+)$", text, re.MULTILINE)
        return f"Wrote **{title}**" + (f" with sections: {', '.join(sections)}." if sections else ".")

    async def after(self, state: dict, text: str, ctx: WorkflowContext[dict, str]) -> None:
        tid = state["ticket_id"]
        existing = re.match(r"\s*Existing article:\s*(KB-\d+)", text, re.IGNORECASE)
        if existing:
            state["mode"] = "fix"
            return
        name = f"KB-draft-{tid}.md"
        result = parse_json(await call_toolbox(ATTACH_TOOL, {
            "ticket_id": tid, "filename": name, "content_base64": base64.b64encode(text.encode("utf-8")).decode(),
        }))
        url = result.get("url", "")
        await call_toolbox(STATUS_TOOL, {"ticket_id": tid, "status": "Escalated"})
        await call_toolbox(NOTE_TOOL, {
            "ticket_id": tid, "author": "case-manager",
            "note": f"[Escalation] No KB article covers this issue. Escalated to an engineer. Draft KB article for review: {url or name}",
        })
        await ctx.yield_output(
            f"**Escalated to an engineer.** {tid} is now *Escalated*. "
            f"The draft article is saved on the ticket for review, not published. [Open the KB draft]({url})\n\n"
        )


class ApprovalGate(Executor):
    """Save the draft on the ticket and stop. Nothing is sent until a human replies "approve"."""

    @handler
    async def request_approval(self, state: dict, ctx: WorkflowContext[Never, str]) -> None:
        try:
            if not await still_current(state["ticket_id"], state.get("run", "")):
                await ctx.yield_output(f"A newer run replaced this one for {state['ticket_id']}, so this draft was not saved.")
                return
            body = draft_body(state["results"].get("drafting", ""))
            await call_toolbox(NOTE_TOOL, {"ticket_id": state["ticket_id"], "author": "case-manager", "note": f"{DRAFT_MARKER}\n{body}"})
        except Exception as e:
            await stop_run(state, "Saving the draft", e)
            raise
        await ctx.yield_output(
            "### Waiting for your approval\n"
            f"The draft is saved on {state['ticket_id']}. Nothing has been sent to the customer.\n\n"
            "Reply **approve** to send it, or **deny** to keep it as a draft.\n"
        )


class ReviseStep(Executor):
    """The engineer asked for changes: rebuild the case from the ticket's saved reports and have the drafting agent rewrite."""

    @handler
    async def revise(self, request: dict, ctx: WorkflowContext[dict, str]) -> None:
        try:
            await self.rebuild(request, ctx)
        except Exception as e:
            await stop_run(request, "Revise", e)
            raise

    async def rebuild(self, request: dict, ctx: WorkflowContext[dict, str]) -> None:
        tid = request["ticket_id"]
        ticket = parse_json(await call_toolbox(GET_TOOL, {"ticket_id": tid}))
        notes = ticket.get("notes", [])
        texts = [n.get("text", "") for n in notes]
        draft_index = max((i for i, t in enumerate(texts) if t.startswith(DRAFT_MARKER)), default=None)
        if draft_index is None:
            await ctx.yield_output(f"There is no draft to revise on {tid}. Start with: New ticket {tid}")
            return
        guidance = next((t[len(CHANGES_MARKER):].strip() for t in reversed(texts[draft_index + 1 :]) if t.startswith(CHANGES_MARKER)), "")
        if not guidance:
            await ctx.yield_output(f"No changes were requested on {tid} since the last draft.")
            return

        def report(title: str) -> str:
            marker = f"{REPORT_MARKER}{title}]"
            return next((t[len(marker):].strip() for t in reversed(texts) if t.startswith(marker)), "")

        has_logs = bool(ticket.get("has_logs"))
        diagnostics = report("Diagnostics")
        mode = "info" if not has_logs else ("escalate" if needs_escalation(diagnostics) else "fix")
        state = case_state(tid, request.get("run", ""))
        state.update(has_logs=has_logs, mode=mode)
        state["results"].update(triage=report("Triage"), diagnostics=diagnostics)
        state["revision"] = (
            "This is a revision. Here is the draft the support engineer reviewed:\n"
            f"{texts[draft_index][len(DRAFT_MARKER):].strip()}\n\n"
            f"The support engineer asked for these changes:\n{guidance}\n\n"
            "Rewrite the draft with these changes. Keep everything else that is still correct. "
            "Use the same format as before, including Why this draft."
        )
        ctx.set_state(LAST_TICKET, tid)
        await ctx.yield_output(f"## Case Manager: {tid}\nRevising the customer draft with the support engineer's guidance.\n\n")
        await ctx.send_message(state)


class DecisionStep(Executor):
    """Act on the support engineer's decision."""

    @handler
    async def decide(self, decision: dict, ctx: WorkflowContext[Never, str]) -> None:
        try:
            await self.act(decision, ctx)
        except Exception as e:
            await stop_run(decision, "Approval", e)
            raise

    async def act(self, decision: dict, ctx: WorkflowContext[Never, str]) -> None:
        ticket = parse_json(await call_toolbox(GET_TOOL, {"ticket_id": decision['ticket_id']}))
        notes = ticket.get("notes", [])
        draft_index = max((i for i, n in enumerate(notes) if n.get("text", "").startswith(DRAFT_MARKER)), default=None)
        if draft_index is None:
            await ctx.yield_output(f"There is no draft waiting for approval on {decision['ticket_id']}. Start with: New ticket {decision['ticket_id']}")
            return
        if any(n.get("type") == "customer_update" for n in notes[draft_index + 1 :]):
            await ctx.yield_output(f"The latest draft for {decision['ticket_id']} was already sent.")
            return

        if decision['approved']:
            message = sign(no_dashes(notes[draft_index]["text"][len(DRAFT_MARKER) :].strip()), decision.get("engineer", ""))
            await call_toolbox(SEND_TOOL, {"ticket_id": decision['ticket_id'], "message": message})
            # An escalated case stays with the engineer; otherwise the ball is in the customer's court.
            status = "Escalated" if ticket.get("status") == "Escalated" else "Awaiting customer"
            await call_toolbox(STATUS_TOOL, {"ticket_id": decision['ticket_id'], "status": status})
            logger.info("Customer update approved and sent for %s", decision['ticket_id'])
            await ctx.yield_output(f"**Approved and sent.** The update went to the customer and {decision['ticket_id']} is now *{status}*.")
        else:
            await call_toolbox(NOTE_TOOL, {
                "ticket_id": decision['ticket_id'],
                "author": "case-manager",
                "note": "[Case Manager] The support engineer did not approve the customer draft. Nothing was sent.",
            })
            await ctx.yield_output(f"**Not sent.** The draft for {decision['ticket_id']} stays on the ticket and the decision was noted.")


def build_agent():
    intake = Intake(id="intake")
    triage = TriageStep("triage", "triage-agent", "Triage", "Triage ticket {ticket_id}. {facts}")
    diagnostics = DiagnosticsStep(
        "diagnostics", "diagnostics-agent", "Diagnostics",
        "Diagnose ticket {ticket_id}. Search the knowledge base for the exact error type before you conclude that no article matches.",
    )
    knowledge = KnowledgeStep(
        "knowledge", "knowledge-agent", "Knowledge gap",
        "Ticket {ticket_id} has no matching KB article. Diagnostics findings:\n{diagnostics}\n\nWrite the draft KB article.",
    )
    drafting = SpecialistStep(
        "drafting", "drafting-agent", "Customer draft",
        "Draft a customer reply for ticket {ticket_id}. Check customer memory for the contact's preferences first. "
        "{mode_rule} "
        "The time now is {now}. {due_rule} "
        "Write any clock time as Eastern time with UTC in parentheses. "
        "Do not reuse times from memory or earlier drafts.",
        customer_facing=True,
    )
    gate = ApprovalGate(id="approval")
    decision = DecisionStep(id="decision")
    revise = ReviseStep(id="revise")
    # The route is decided in code from facts (logs attached? KB matched?), not by a model.
    workflow = (
        WorkflowBuilder(start_executor=intake, name="case-manager")
        .add_edge(intake, triage, condition=lambda m: m.get("kind") == "case")
        .add_edge(intake, decision, condition=lambda m: m.get("kind") == "decision")
        .add_edge(intake, revise, condition=lambda m: m.get("kind") == "revise")
        .add_edge(revise, drafting)
        .add_edge(triage, diagnostics, condition=lambda s: s.get("has_logs", True))
        .add_edge(triage, drafting, condition=lambda s: not s.get("has_logs", True))
        .add_edge(diagnostics, knowledge, condition=lambda s: s.get("mode") == "escalate")
        .add_edge(diagnostics, drafting, condition=lambda s: s.get("mode") != "escalate")
        .add_edge(knowledge, drafting)
        .add_edge(drafting, gate)
        .build()
    )
    return workflow.as_agent(name="case-manager")


class InMemoryCheckpoints(ContextScopedStoreProvider):
    """Keep workflow checkpoints in memory instead of Foundry storage.

    Every run starts fresh from the ticket system (no conversation or previous_response_id),
    so durable checkpoints are never read back. Saving them remotely only added a call at
    every step that could time out (408) and kill the run.
    """

    def get_store(self, *, config, context_id, platform_context):
        return InMemoryCheckpointStorage()


def main():
    ResponsesHostServer(build_agent(), checkpoint_store_provider=InMemoryCheckpoints()).run()


if __name__ == "__main__":
    main()
