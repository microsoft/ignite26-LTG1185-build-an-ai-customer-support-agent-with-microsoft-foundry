"""Business logic for the mock ticket service, kept separate from Azure Functions so it can be unit tested."""

import base64
import binascii
import json
import random
import re
import time
import uuid
from datetime import datetime, timedelta, timezone

import seed_data

TICKETS_TABLE = "tickets"
NOTES_TABLE = "notes"
FILES_TABLE = "files"
FILE_CHUNK = 60_000  # Table Storage binary properties max out at 64 KB, so files are split into chunks.
MAX_FILE_BYTES = 800_000
FILE_TYPES = {
    ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".csv": "text/csv", ".txt": "text/plain",
    ".md": "text/markdown",
}
TICKET_PARTITION = "ticket"

VALID_STATUSES = ["New", "Triaged", "In progress", "Awaiting customer", "Escalated", "Resolved", "Closed"]
VALID_SEVERITIES = ["Sev1", "Sev2", "Sev3", "Sev4"]
CLOSED_STATUSES = {"Resolved", "Closed"}
NOTE_TYPES = {"internal", "customer_update"}

# Markers shared with the Case Manager (case-manager/src/case-manager/main.py), used by the support console.
DRAFT_MARKER = "[Draft awaiting approval]"
DENIED_MARKER = "[Case Manager] The support engineer did not approve"
DISPATCH_MARKER = "[Sent to Case Manager]"
CHANGES_MARKER = "[Changes requested]"
# Run tracking. Every request carries a token, e.g. "New ticket TCK-1042 (run 3f9a2c)":
#   "[Sent to Case Manager]" / "[Case Manager restarted]"  a run was requested (by a person / by the watchdog)
#   "[Case Manager started] run 3f9a2c ..."                the Case Manager picked it up
#   "[Run stopped] run 3f9a2c: ..."                        the run gave up (Foundry kept timing out); restart it
#   "[Run failed] ..."                                     no more automatic restarts; the console offers Retry
RESTART_MARKER = "[Case Manager restarted]"
STARTED_MARKER = "[Case Manager started]"
STOPPED_MARKER = "[Run stopped]"
FAILED_MARKER = "[Run failed]"
RUN_TOKEN = re.compile(r"\s*\(run ([a-z0-9]+)\)", re.IGNORECASE)
START_TIMEOUT = timedelta(minutes=2, seconds=30)  # requested, but the Case Manager never picked it up
IDLE_TIMEOUT = timedelta(minutes=4)  # started, but nothing new on the ticket for this long
MAX_RESTARTS = 2
WORKING_TIMEOUT = timedelta(minutes=10)  # backstop if the watchdog itself isn't running


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


_last_seq = 0


def _seq() -> str:
    """Strictly increasing nanosecond stamp, so notes written in the same second keep their order."""
    global _last_seq
    _last_seq = max(time.time_ns(), _last_seq + 1)
    return f"{_last_seq:020d}"


def _no_dashes(text: str) -> str:
    """Agents write notes straight through the tools, so the house style (no em/en dashes) is enforced here too."""
    text = re.sub(r"(\d)\s*[\u2013\u2014]\s*(\d)", r"\1 to \2", text)
    text = re.sub(r"\s*[\u2014\u2013]\s*(?=\w)", ", ", text)
    return text.replace("\u2014", "-").replace("\u2013", "-")


def _is_eval(entity) -> bool:
    return entity["RowKey"].startswith("EVAL-")


def _parse(value: str) -> datetime:
    return datetime.strptime(value, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)


def new_run_token() -> str:
    return uuid.uuid4().hex[:6]


def with_token(message: str, token: str) -> str:
    """"New ticket TCK-1042" -> "New ticket TCK-1042 (run 3f9a2c)" (replacing any older token)."""
    return f"{RUN_TOKEN.sub('', message).strip()} (run {token})"


def run_status(notes, now):
    """The latest Case Manager run on a ticket, read from its notes. None if it was never sent to the Case Manager."""
    def last(match, after=-1):
        return max((i for i, n in enumerate(notes) if i > after and match(n)), default=-1)

    def starts(marker):
        return lambda n: n["text"].startswith(marker)

    request = last(lambda n: n["text"].startswith(DISPATCH_MARKER) or n["text"].startswith(RESTART_MARKER))
    if request < 0:
        return None
    quoted = re.search(r'"([^"]*)"', notes[request]["text"])
    message = quoted.group(1) if quoted else ""
    found = RUN_TOKEN.search(message)
    token = found.group(1).lower() if found else ""

    def mine(marker):
        return lambda n: bool(token) and n["text"].startswith(marker) and f"run {token}" in n["text"].lower()

    origin = last(starts(DISPATCH_MARKER))
    finished = max(last(starts(DRAFT_MARKER)), last(lambda n: n["type"] == "customer_update" or n["text"].startswith(DENIED_MARKER)))
    failed = last(starts(FAILED_MARKER), request)
    stopped = last(mine(STOPPED_MARKER), request)
    requested_at = _parse(notes[request]["created_at"])
    if finished > request:
        state, error = "done", None
    elif failed >= 0:
        state, error = "failed", notes[failed]["text"][len(FAILED_MARKER):].strip()
    elif now - requested_at >= WORKING_TIMEOUT:
        state, error = "failed", "The run didn't finish within 10 minutes."
    else:
        state, error = "working", None
    return {
        "state": state, "error": error, "message": message, "token": token,
        "requested_at": requested_at, "last_activity": _parse(notes[-1]["created_at"]),
        "started": last(mine(STARTED_MARKER), request) >= 0,
        "stopped": notes[stopped]["text"].split(":", 1)[-1].strip() if stopped >= 0 else None,
        "restarts": sum(1 for n in notes[origin + 1 :] if n["text"].startswith(RESTART_MARKER)) if origin >= 0 else 0,
        "failed_noted": failed >= 0,
    }


def watchdog_action(notes, now):
    """Decide what to do about a run that died: ("restart", reason, new message), ("fail", reason), or None.

    Foundry can time out (408) before a run starts or in the middle of a step. A run that gave up, never
    started, or went quiet is sent again with a new token, up to MAX_RESTARTS times; then it is marked failed.
    """
    s = run_status(notes, now)
    if not s:
        return None
    if s["state"] == "failed" and not s["failed_noted"]:
        return ("fail", s["error"])
    if s["state"] != "working":
        return None
    if s["stopped"]:
        reason = s["stopped"]
    elif not s["started"] and now - s["requested_at"] >= START_TIMEOUT:
        reason = "Foundry didn't start the run."
    elif s["started"] and now - s["last_activity"] >= IDLE_TIMEOUT:
        reason = "The run stopped making progress."
    else:
        return None
    if s["restarts"] < MAX_RESTARTS and s["message"]:
        return ("restart", reason, with_token(s["message"], new_run_token()))
    return ("fail", reason)


class MemoryStore:
    """In-memory stand-in for Azure Table Storage (used by unit tests)."""

    def __init__(self):
        self.tables = {}

    def upsert(self, table, entity):
        self.tables.setdefault(table, {})[(entity["PartitionKey"], entity["RowKey"])] = dict(entity)

    def get(self, table, pk, rk):
        item = self.tables.get(table, {}).get((pk, rk))
        return dict(item) if item else None

    def query_partition(self, table, pk):
        return [dict(e) for (p, _), e in self.tables.get(table, {}).items() if p == pk]

    def list_all(self, table):
        return [dict(e) for e in self.tables.get(table, {}).values()]

    def clear(self, table):
        self.tables[table] = {}


class TicketService:
    def __init__(self, store, clock=_now):
        self.store = store
        self.clock = clock

    # ---------- helpers ----------
    def _ticket(self, ticket_id):
        ticket_id = (ticket_id or "").strip().upper()
        entity = self.store.get(TICKETS_TABLE, TICKET_PARTITION, ticket_id)
        if not entity:
            raise ValueError(f"Ticket '{ticket_id}' was not found.")
        return entity

    def _add_note(self, ticket_id, note_type, text, author):
        now = self.clock()
        note = {
            "PartitionKey": ticket_id,
            "RowKey": f"{_iso(now)}-{_seq()}-{uuid.uuid4().hex[:6]}",
            "type": note_type,
            "author": author,
            "text": text,
            "created_at": _iso(now),
        }
        self.store.upsert(NOTES_TABLE, note)
        return note

    def _touch(self, ticket):
        ticket["updated_at"] = _iso(self.clock())
        self.store.upsert(TICKETS_TABLE, ticket)

    @staticmethod
    def _profile(entity):
        """Seed tickets use their own ID as the log profile; tickets created from an example name one."""
        return entity.get("log_profile") or entity["RowKey"]

    def _logs(self, entity):
        return generate_logs(entity["RowKey"], _parse(entity["created_at"]), self._profile(entity))

    def _notes(self, ticket_id):
        return sorted(self.store.query_partition(NOTES_TABLE, ticket_id), key=lambda n: n["RowKey"])

    @staticmethod
    def _public_ticket(entity):
        customer = seed_data.CUSTOMERS.get(entity["customer_id"], {})
        return {
            "ticket_id": entity["RowKey"],
            "title": entity["title"],
            "description": entity["description"],
            "status": entity["status"],
            "severity": entity.get("severity") or "Not set",
            "product": entity["product"],
            "product_version": entity["product_version"] or "Not provided",
            "environment": entity["environment"],
            "created_at": entity["created_at"],
            "updated_at": entity["updated_at"],
            "has_logs": bool(entity.get("has_logs")),
            "customer": {
                "customer_id": entity["customer_id"],
                "name": customer.get("name"),
                "tier": customer.get("tier"),
                "contact_name": customer.get("contact_name"),
                "contact_email": customer.get("contact_email"),
            },
        }

    # ---------- tools ----------
    def get_ticket(self, ticket_id):
        entity = self._ticket(ticket_id)
        result = self._public_ticket(entity)
        notes = self._notes(entity["RowKey"])
        result["notes"] = [
            {"type": n["type"], "author": n["author"], "created_at": n["created_at"], "text": n["text"]}
            for n in notes
        ]
        return result

    def get_logs(self, ticket_id, max_lines=200):
        entity = self._ticket(ticket_id)
        if not entity.get("has_logs"):
            return {"ticket_id": entity["RowKey"], "log_lines": 0, "logs": "No logs are attached to this ticket."}
        max_lines = max(20, min(int(max_lines or 200), 400))
        lines = self._logs(entity)
        return {"ticket_id": entity["RowKey"], "log_lines": min(len(lines), max_lines), "logs": "\n".join(lines[:max_lines])}

    def get_log_stats(self, ticket_id):
        """Pre-aggregated log analysis, like a log analytics query: exact counts without returning every line."""
        entity = self._ticket(ticket_id)
        tid = entity["RowKey"]
        if not entity.get("has_logs"):
            return {"ticket_id": tid, "total_lines": 0, "summary": "No logs are attached to this ticket."}
        lines = self._logs(entity)
        profile = seed_data.LOG_PROFILES[self._profile(entity)]
        parsed = []
        for line in lines:
            ts, level, svc, msg = line.split(None, 3)
            parsed.append((ts, level, msg))

        by_level = {}
        for _, level, _ in parsed:
            by_level[level] = by_level.get(level, 0) + 1

        errors = [p for p in parsed if p[1] == "ERROR"]
        warns = [p for p in parsed if p[1] == "WARN"]
        by_type = {}
        for _, _, msg in errors:
            sig = msg.split(" (")[0].strip()
            by_type[sig] = by_type.get(sig, 0) + 1

        hours = {}
        for ts, level, _ in parsed:
            bucket = hours.setdefault(ts[:13] + ":00Z", {"errors": 0, "warnings": 0, "total": 0})
            bucket["total"] += 1
            if level == "ERROR":
                bucket["errors"] += 1
            elif level == "WARN":
                bucket["warnings"] += 1
        by_hour = [{"hour": h, **c} for h, c in sorted(hours.items())]
        peak = max(by_hour, key=lambda b: b["errors"]) if errors else None
        change = next((l for l in lines if profile["change_event"] in l), None)

        return {
            "ticket_id": tid,
            "service": profile["service"],
            "time_range": {"first": parsed[0][0], "last": parsed[-1][0]},
            "total_lines": len(parsed),
            "counts_by_level": by_level,
            "change_event": change,
            "errors": {
                "total": len(errors),
                "first_at": errors[0][0] if errors else None,
                "last_at": errors[-1][0] if errors else None,
                "by_type": [{"type": k, "count": v} for k, v in sorted(by_type.items(), key=lambda kv: -kv[1])],
            },
            "errors_by_hour": by_hour,
            "peak_error_hour": {"hour": peak["hour"], "errors": peak["errors"]} if peak else None,
            "sample_lines": {
                "first_error": next((l for l in lines if " ERROR " in l), None),
                "first_warning": next((l for l in lines if " WARN " in l), None),
            },
        }

    def add_ticket_note(self, ticket_id, note, author="agent"):
        if not (note or "").strip():
            raise ValueError("The note text is empty.")
        entity = self._ticket(ticket_id)
        self._add_note(entity["RowKey"], "internal", _no_dashes(note.strip()), author or "agent")
        self._touch(entity)
        return {"ticket_id": entity["RowKey"], "result": "Internal note added."}

    def update_ticket_status(self, ticket_id, status, severity=None):
        entity = self._ticket(ticket_id)
        match = next((s for s in VALID_STATUSES if s.lower() == (status or "").strip().lower()), None)
        if not match:
            raise ValueError(f"Invalid status '{status}'. Use one of: {', '.join(VALID_STATUSES)}.")
        changes = []
        if entity["status"] != match:
            changes.append(f"status {entity['status']} -> {match}")
            entity["status"] = match
        if severity:
            sev = next((s for s in VALID_SEVERITIES if s.lower() == severity.strip().lower()), None)
            if not sev:
                raise ValueError(f"Invalid severity '{severity}'. Use one of: {', '.join(VALID_SEVERITIES)}.")
            if (entity.get("severity") or "") != sev:
                changes.append(f"severity {entity.get('severity') or 'Not set'} -> {sev}")
                entity["severity"] = sev
        if changes:
            self._add_note(entity["RowKey"], "internal", "Ticket updated: " + "; ".join(changes), "system")
            self._touch(entity)
        return {"ticket_id": entity["RowKey"], "status": entity["status"], "severity": entity.get("severity") or "Not set"}

    def list_open_tickets(self, status=None):
        now = self.clock()
        tickets = []
        for e in self.store.list_all(TICKETS_TABLE):
            if e["status"] in CLOSED_STATUSES or _is_eval(e):
                continue
            if status and e["status"].lower() != status.strip().lower():
                continue
            hours = round((now - _parse(e["updated_at"])).total_seconds() / 3600, 1)
            tickets.append({
                "ticket_id": e["RowKey"],
                "title": e["title"],
                "customer": seed_data.CUSTOMERS.get(e["customer_id"], {}).get("name"),
                "status": e["status"],
                "severity": e.get("severity") or "Not set",
                "hours_since_last_update": hours,
            })
        tickets.sort(key=lambda t: t["hours_since_last_update"], reverse=True)
        return {"count": len(tickets), "tickets": tickets}

    def send_customer_update(self, ticket_id, message):
        if not (message or "").strip():
            raise ValueError("The message is empty.")
        entity = self._ticket(ticket_id)
        customer = seed_data.CUSTOMERS.get(entity["customer_id"], {})
        text = _no_dashes(message.strip())
        # The sender is whoever signed the approved email; unsigned mail comes from the team.
        signer = re.search(r"^\s*([^,\n\[\]]+), Contoso OrderSync Support\s*$", text, re.MULTILINE)
        self._add_note(entity["RowKey"], "customer_update", text, signer.group(1).strip() if signer else "support-team")
        self._touch(entity)
        return {
            "ticket_id": entity["RowKey"],
            "result": "Customer update sent (simulated - no real email is sent).",
            "recipient": f"{customer.get('contact_name')} <{customer.get('contact_email')}>",
        }

    def case_history(self, ticket_id, event):
        """The customer-memory entry for a case lifecycle event, built from ticket data (no model involved).

        Returns (memory scope, ticket id, content). One entry per ticket: later events replace earlier ones.
        """
        ticket = self.get_ticket(ticket_id)
        diagnostics = " ".join(n["text"] for n in ticket["notes"] if "[Diagnostics]" in n["text"])
        kb = sorted(set(re.findall(r"\bKB-\d{3}\b", diagnostics)))
        version = ticket["product_version"]
        text = f"{ticket['ticket_id']} ({self.clock().strftime('%b %d, %Y')}): {ticket['title']}"
        if version != "Not provided":
            text += f" (OrderSync {version})"
        text += ". "
        if "Needs engineer review" in diagnostics:
            text += "Root cause not confirmed; escalated to an engineer. "
        elif kb:
            text += f"Root cause matched {', '.join(kb)}. "
        text += "Resolved." if event == "resolved" else "Customer update sent."
        return ticket["customer"]["customer_id"], ticket["ticket_id"], _no_dashes(text)

    def attach_file(self, ticket_id, filename, content_base64):
        entity = self._ticket(ticket_id)
        name = re.sub(r"[^A-Za-z0-9._-]", "_", (filename or "").strip())[:80]
        ext = name[name.rfind("."):].lower() if "." in name else ""
        if ext not in FILE_TYPES:
            raise ValueError(f"Unsupported file type. Allowed: {', '.join(sorted(FILE_TYPES))}.")
        try:
            data = base64.b64decode(content_base64 or "", validate=True)
        except (binascii.Error, ValueError):
            raise ValueError("content_base64 is not valid base64.")
        if not data:
            raise ValueError("The file is empty.")
        if len(data) > MAX_FILE_BYTES:
            raise ValueError(f"The file is too large (max {MAX_FILE_BYTES // 1000} KB).")
        record = {"PartitionKey": entity["RowKey"], "RowKey": name, "content_type": FILE_TYPES[ext], "size": len(data)}
        chunks = [data[i : i + FILE_CHUNK] for i in range(0, len(data), FILE_CHUNK)]
        record["chunks"] = len(chunks)
        record.update({f"chunk_{i:02d}": c for i, c in enumerate(chunks)})
        self.store.upsert(FILES_TABLE, record)
        self._touch(entity)
        return {"ticket_id": entity["RowKey"], "filename": name, "size": len(data), "path": f"/api/files/{entity['RowKey']}/{name}"}

    def get_file(self, ticket_id, filename):
        record = self.store.get(FILES_TABLE, (ticket_id or "").strip().upper(), filename or "")
        if not record:
            return None
        data = b"".join(bytes(record[f"chunk_{i:02d}"]) for i in range(int(record["chunks"])))
        return data, record["content_type"]

    # ---------- support console (not MCP tools: people use these, agents don't) ----------
    def create_ticket(self, customer_id, title, description, product_version="", has_logs=False, log_profile=None):
        """A customer submits a new case, like a support portal form would."""
        if customer_id not in seed_data.CUSTOMERS:
            raise ValueError(f"Unknown customer '{customer_id}'.")
        title, description = (title or "").strip(), (description or "").strip()
        if not title or not description:
            raise ValueError("A title and a description are required.")
        if has_logs and log_profile not in seed_data.LOG_PROFILES:
            raise ValueError("Attached logs need a known log profile.")
        numbers = [int(e["RowKey"][4:]) for e in self.store.list_all(TICKETS_TABLE) if re.fullmatch(r"TCK-\d+", e["RowKey"])]
        ticket_id = f"TCK-{max(numbers + [1046]) + 1}"
        now = _iso(self.clock())
        entity = {
            "PartitionKey": TICKET_PARTITION, "RowKey": ticket_id, "customer_id": customer_id,
            "title": title[:200], "description": description[:4000], "product": "OrderSync",
            "product_version": (product_version or "").strip()[:20], "environment": "Production",
            "status": "New", "severity": "", "created_at": now, "updated_at": now, "has_logs": bool(has_logs),
        }
        if has_logs:
            entity["log_profile"] = log_profile
        self.store.upsert(TICKETS_TABLE, entity)
        self._add_note(ticket_id, "internal", "Ticket submitted by the customer through the support portal.", "ticket-system")
        return self._public_ticket(entity)

    def create_from_example(self, example):
        template = seed_data.EXAMPLES.get(example)
        if not template:
            raise ValueError(f"Unknown example '{example}'.")
        return self.create_ticket(**{k: v for k, v in template.items() if k != "label"})

    @staticmethod
    def automation_state(notes, now):
        """What the console shows: a draft waiting for a human, or the Case Manager still working."""
        def last(match):
            return max((i for i, n in enumerate(notes) if match(n)), default=-1)

        draft = last(lambda n: n["text"].startswith(DRAFT_MARKER))
        decided = last(lambda n: n["type"] == "customer_update" or n["text"].startswith(DENIED_MARKER))
        pending = draft >= 0 and decided < draft
        run = run_status(notes, now)
        working = bool(run) and run["state"] == "working"
        failed = bool(run) and run["state"] == "failed"
        return {
            "awaiting_approval": pending and not working,
            "draft": notes[draft]["text"][len(DRAFT_MARKER):].strip() if pending else None,
            "draft_author": notes[draft].get("author") if pending else None,
            "agent_working": working,
            "run_failed": failed,
            "run_error": run["error"] if failed else None,
        }

    def save_engineer_edit(self, ticket_id, text, who):
        """The engineer edited the draft before approving: save it as the latest draft, so that is what gets sent."""
        text = (text or "").strip()
        if not text:
            raise ValueError("The edited email is empty.")
        entity = self._ticket(ticket_id)
        self._add_note(entity["RowKey"], "internal", f"{DRAFT_MARKER}\n{text}", who)
        self._touch(entity)

    def request_changes(self, ticket_id, guidance, who):
        """The engineer's guidance for the drafting agent, kept on the ticket for the record."""
        guidance = (guidance or "").strip()
        if not guidance:
            raise ValueError("Tell the agents what to change.")
        entity = self._ticket(ticket_id)
        self._add_note(entity["RowKey"], "internal", f"{CHANGES_MARKER} {guidance}", who)
        self._touch(entity)

    def console_ticket(self, ticket_id):
        result = self.get_ticket(ticket_id)
        result.update(self.automation_state(result["notes"], self.clock()))
        return result

    def console_list(self):
        now = self.clock()
        items = []
        for e in self.store.list_all(TICKETS_TABLE):
            if _is_eval(e) or e["status"] in CLOSED_STATUSES:
                continue
            state = self.automation_state(
                [{"type": n["type"], "text": n["text"], "created_at": n["created_at"]} for n in self._notes(e["RowKey"])], now
            )
            customer = seed_data.CUSTOMERS.get(e["customer_id"], {})
            items.append({
                "ticket_id": e["RowKey"], "title": e["title"], "customer": customer.get("name"), "tier": customer.get("tier"),
                "status": e["status"], "severity": e.get("severity") or "", "updated_at": e["updated_at"],
                "awaiting_approval": state["awaiting_approval"], "agent_working": state["agent_working"],
                "run_failed": state["run_failed"],
            })
        items.sort(key=lambda t: t["updated_at"], reverse=True)
        return {"count": len(items), "tickets": items}

    def record_dispatch(self, ticket_id, message, routine, dispatch_id=None, who="ticket-system"):
        """Audit trail on the ticket: what was sent to Foundry, through which routine, and by whom.

        Written the moment the run is requested, so the console's timer starts when the button is clicked.
        """
        entity = self._ticket(ticket_id)
        ref = f" (dispatch {dispatch_id})" if dispatch_id else ""
        self._add_note(entity["RowKey"], "internal", f'{DISPATCH_MARKER} "{message}" via Foundry routine {routine}{ref}.', who)
        self._touch(entity)

    def record_restart(self, ticket_id, message, routine, reason):
        entity = self._ticket(ticket_id)
        self._add_note(entity["RowKey"], "internal", f'{RESTART_MARKER} "{message}" via Foundry routine {routine}. {reason}', "ticket-system")
        self._touch(entity)

    def record_stopped(self, ticket_id, token, reason):
        self._add_note(self._ticket(ticket_id)["RowKey"], "internal", f"{STOPPED_MARKER} run {token}: {reason}", "ticket-system")

    def record_failed(self, ticket_id, reason):
        entity = self._ticket(ticket_id)
        self._add_note(entity["RowKey"], "internal", f"{FAILED_MARKER} {reason} Select Retry to run it again.", "ticket-system")
        self._touch(entity)

    def retry_message(self, ticket_id):
        """The request to send again when the engineer selects Retry: the latest run's, without its token."""
        entity = self._ticket(ticket_id)
        run = run_status(self.get_ticket(entity["RowKey"])["notes"], self.clock())
        if not run or run["state"] != "failed" or not run["message"]:
            raise ValueError(f"{entity['RowKey']} has no failed run to retry.")
        return RUN_TOKEN.sub("", run["message"]).strip()

    def watchdog_actions(self):
        """Open tickets whose run died: [(ticket_id, action)] where action comes from watchdog_action."""
        now = self.clock()
        actions = []
        for e in self.store.list_all(TICKETS_TABLE):
            if _is_eval(e) or e["status"] in CLOSED_STATUSES:
                continue
            notes = [{"type": n["type"], "text": n["text"], "created_at": n["created_at"]} for n in self._notes(e["RowKey"])]
            action = watchdog_action(notes, now)
            if action:
                actions.append((e["RowKey"], action))
        return actions

    # ---------- admin ----------
    def reset_demo(self):
        now = self.clock()
        self.store.clear(TICKETS_TABLE)
        self.store.clear(NOTES_TABLE)
        self.store.clear(FILES_TABLE)
        for t in seed_data.TICKETS + seed_data.EVAL_TICKETS:
            entity = {k: v for k, v in t.items() if k not in ("ticket_id", "created_hours_ago", "updated_hours_ago")}
            entity.update({
                "PartitionKey": TICKET_PARTITION,
                "RowKey": t["ticket_id"],
                "created_at": _iso(now - timedelta(hours=t["created_hours_ago"])),
                "updated_at": _iso(now - timedelta(hours=t["updated_hours_ago"])),
            })
            self.store.upsert(TICKETS_TABLE, entity)
        for i, n in enumerate(seed_data.NOTES + seed_data.EVAL_NOTES):
            created = now - timedelta(hours=n["hours_ago"])
            self.store.upsert(NOTES_TABLE, {
                "PartitionKey": n["ticket_id"],
                "RowKey": f"{_iso(created)}-seed{i}",
                "type": n["type"],
                "author": n["author"],
                "text": n["text"],
                "created_at": _iso(created),
            })
        return {
            "result": "Demo data reset.", "tickets": len(seed_data.TICKETS),
            "eval_tickets": len(seed_data.EVAL_TICKETS), "notes": len(seed_data.NOTES),
        }


def generate_logs(ticket_id, created_at, profile_key=None):
    """Deterministic, realistic-looking logs: healthy before the change event, failing after it."""
    profile = seed_data.LOG_PROFILES.get(profile_key or ticket_id)
    if not profile:
        return []
    rng = random.Random(ticket_id)
    change_at = created_at - timedelta(hours=profile["hours_after_change"])
    start = change_at - timedelta(hours=profile["hours_before_change"])
    end = created_at
    svc = profile["service"]
    lines = []
    t = start
    change_logged = False
    while t < end:
        if not change_logged and t >= change_at:
            lines.append(f"{_iso(change_at)} INFO  {svc} {profile['change_event']}")
            change_logged = True
        after = t >= change_at
        # Busier (and more failures) in the middle of the post-change window, like a lunchtime peak.
        peak = after and abs((t - change_at).total_seconds() / 3600 - profile["hours_after_change"] / 2) < 1.25
        for _ in range(2):
            ts = _iso(t + timedelta(seconds=rng.randint(0, 299)))
            roll = rng.random()
            error_rate = profile["error_rate_after"] * (1.6 if peak else 0.6) if after else 0.0
            if roll < error_rate:
                lines.append(f"{ts} ERROR {svc} " + profile["error_line"].format(w=rng.randint(3, 40), u=rng.randint(17, 20)))
            elif after and roll < error_rate + 0.15:
                lines.append(f"{ts} WARN  {svc} " + profile["warn_line"].format(w=rng.randint(3, 40), u=rng.randint(17, 20)))
            else:
                lines.append(f"{ts} INFO  {svc} " + profile["info_line"].format(d=rng.randint(80, 260)))
        t += timedelta(minutes=5)
    lines.sort()
    return lines


def to_json(result) -> str:
    return json.dumps(result, indent=2)
