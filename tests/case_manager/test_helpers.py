"""Unit tests for the Case Manager's deterministic helpers: the parts that are policy in code, not in a prompt."""

import os
import sys
import unittest
from datetime import datetime, timezone

os.environ.setdefault("FOUNDRY_PROJECT_ENDPOINT", "https://example.services.ai.azure.com/api/projects/test")
os.environ.setdefault("TOOLBOX_ENDPOINT", "https://example.services.ai.azure.com/api/projects/test/toolboxes/t/mcp")
os.environ.setdefault("KB_BASE_URL", "https://tickets.example.net/api/kb")
sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "case-manager"))

import main  # noqa: E402

DRAFT = (
    "**Body:**\nHi Dana,\n\nThe pool limit was lowered in v4.2 (see KB-104). Set ORDERSYNC_POOL_SIZE=100.\n"
    "We will send the next update by 11:00 AM ET.\n\n[Engineer name], Contoso OrderSync Support\n\n"
    "**Why this draft:** Fix from KB-104, timing from KB-107."
)


class SignatureTests(unittest.TestCase):
    def test_sign_with_engineer(self):
        self.assertEqual(main.sign("Thanks,\n[Engineer name], Support", "Alex Rivera"), "Thanks,\nAlex Rivera, Support")

    def test_sign_without_engineer_signs_as_team(self):
        self.assertEqual(main.sign("Thanks,\n[Engineer name], Support", ""), "Thanks,\nSupport")

    def test_approver_from_console(self):
        self.assertEqual(main.approver("approve TCK-1045 by Alex Rivera"), "Alex Rivera")

    def test_approver_from_chat_has_no_name(self):
        self.assertEqual(main.approver("approve"), "")


class DecisionPatternTests(unittest.TestCase):
    def test_only_a_queue_request_picks_a_ticket_on_its_own(self):
        self.assertTrue(main.QUEUE_PICKUP.search("Process the oldest New ticket"))
        message = "New ticket INC0012345"
        self.assertFalse(main.TICKET_ID.search(message) or main.QUEUE_PICKUP.search(message))

    def test_approve_variants(self):
        for text in ("approve", "Approved", "send it", "yes, go ahead"):
            self.assertTrue(main.APPROVE.match(text), text)

    def test_deny_and_revise(self):
        self.assertTrue(main.DENY.match("deny TCK-1042"))
        self.assertTrue(main.REVISE.match("revise: shorter please"))
        self.assertFalse(main.APPROVE.match("not yet"))


class TextPolicyTests(unittest.TestCase):
    def test_no_dashes(self):
        self.assertEqual(main.no_dashes("5\u201315 minutes"), "5 to 15 minutes")
        self.assertEqual(main.no_dashes("the fix \u2014 a restart"), "the fix, a restart")

    def test_internal_kb_removed_from_email_but_linked_in_notes(self):
        out = main.tidy_draft(DRAFT)
        email, notes = out[: main.email_end(out)], out[main.email_end(out):]
        self.assertNotIn("KB-104", email)
        self.assertIn("[KB-104](https://tickets.example.net/api/kb/KB-104)", notes)
        self.assertIn("[KB-107](https://tickets.example.net/api/kb/KB-107)", notes)

    def test_customer_kb_stays_in_email_as_link(self):
        draft = "**Body:**\nPlease follow KB-109 to send logs.\n\n**Why this draft:** Missing info."
        self.assertIn("[KB-109](https://tickets.example.net/api/kb/KB-109)", main.tidy_draft(draft))

    def test_retrieved_sources_added_to_notes(self):
        out = main.tidy_draft("**Body:**\nHi.\n\n**Why this draft:** Based on the article.", sources=["KB-105"])
        self.assertIn("Sources: [KB-105]", out)

    def test_enforce_due_rewrites_only_update_lines(self):
        email = "The outage began at 9:00 AM ET.\nWe will send an update by 10:30 AM ET."
        out = main.enforce_due(email, "11:00 AM ET (15:00 UTC)")
        self.assertIn("began at 9:00 AM ET", out)
        self.assertIn("update by 11:00 AM ET (15:00 UTC)", out)

    def test_plain_time(self):
        self.assertEqual(main.plain_time(datetime(2026, 11, 18, 16, 50, tzinfo=timezone.utc)), "11:50 AM ET (16:50 UTC)")


class RoutingTests(unittest.TestCase):
    def test_update_due_premier_sev1_is_a_clock_time(self):
        instruction, due = main.update_due("**Severity:** Sev1. Premier customer.")
        self.assertIsNotNone(due)
        self.assertIn("Sev1 Premier", instruction)

    def test_update_due_standard_sev3_is_business_hours(self):
        instruction, due = main.update_due("**Severity:** Sev3. Standard support plan.")
        self.assertIsNone(due)
        self.assertIn("1 business day", instruction)

    def test_known_issue_with_high_confidence_is_not_escalated(self):
        self.assertFalse(main.needs_escalation("**Known issue:** KB-104 pool exhaustion\n**Confidence:** High"))

    def test_no_match_or_low_confidence_escalates(self):
        self.assertTrue(main.needs_escalation("**Known issue:** No matching article\n**Confidence:** Medium"))
        self.assertTrue(main.needs_escalation("**Known issue:** KB-104\n**Confidence:** Low"))


class RunTrackingTests(unittest.TestCase):
    def test_latest_run_is_newest_request(self):
        notes = [
            {"text": "[Sent to Case Manager] (run abc123)"},
            {"text": "[Case Manager started]"},
            {"text": "[Case Manager restarted] (run def456)"},
            {"text": "Triage done"},
        ]
        self.assertEqual(main.latest_run(notes), "def456")

    def test_is_transient_follows_exception_chain(self):
        try:
            try:
                raise TimeoutError("408 RequestTimeout")
            except TimeoutError as inner:
                raise ValueError("tool call failed") from inner
        except ValueError as outer:
            self.assertTrue(main.is_transient(outer))
        self.assertFalse(main.is_transient(KeyError("ticket_id")))


if __name__ == "__main__":
    unittest.main()
