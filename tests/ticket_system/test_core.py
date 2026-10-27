import json
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "src", "ticket-system"))

from tickets_core import MemoryStore, TicketService, to_json  # noqa: E402


class TicketServiceTests(unittest.TestCase):
    def setUp(self):
        self.svc = TicketService(MemoryStore())
        self.svc.reset_demo()

    def test_hero_ticket(self):
        t = self.svc.get_ticket("TCK-1042")
        text = to_json(t)
        self.assertIn("Contoso", text)
        self.assertIn("Dana", text)
        self.assertIn("4.2", text)
        json.loads(text)

    def test_ticket_id_is_case_insensitive(self):
        self.assertEqual(self.svc.get_ticket("tck-1042")["ticket_id"], "TCK-1042")

    def test_unknown_ticket(self):
        with self.assertRaises(ValueError):
            self.svc.get_ticket("TCK-9999")

    def test_eval_tickets_are_readable_but_hidden(self):
        self.assertEqual(self.svc.get_ticket("eval-01")["customer"]["tier"], "Premier")
        visible = [t["ticket_id"] for t in self.svc.console_list()["tickets"]]
        visible += [t["ticket_id"] for t in self.svc.list_open_tickets()["tickets"]]
        self.assertFalse([i for i in visible if i.startswith("EVAL-")])
        self.assertEqual(self.svc.create_ticket("CUST-CONTOSO", "t", "d")["ticket_id"], "TCK-1047")

    def test_drafting_eval_tickets_arrive_triaged_and_diagnosed(self):
        t = self.svc.get_ticket("EVAL-11")
        self.assertEqual((t["status"], t["severity"]), ("Triaged", "Sev2"))
        self.assertEqual([n["text"][:13] for n in t["notes"]], ["[Triage] Sev2", "[Diagnostics]"])
        self.assertIn("ConnectionPoolExhausted", to_json(self.svc.get_log_stats("EVAL-11")))
        self.assertFalse(self.svc.get_ticket("EVAL-13")["has_logs"])
        self.assertIn("fix.exe", self.svc.get_ticket("EVAL-23")["description"])

    def test_notes_drop_dashes_and_noop_status_is_silent(self):
        self.svc.add_ticket_note("TCK-1045", "Orders stuck \u2014 missing logs; 10\u201312 UTC", "triage-agent")
        self.svc.update_ticket_status("TCK-1045", "Escalated")
        before = len(self.svc.get_ticket("TCK-1045")["notes"])
        self.svc.update_ticket_status("TCK-1045", "Escalated")
        notes = self.svc.get_ticket("TCK-1045")["notes"]
        self.assertEqual(before, len(notes))
        texts = " ".join(n["text"] for n in notes)
        self.assertIn("Orders stuck, missing logs; 10 to 12 UTC", texts)
        self.assertNotIn("\u2014", texts)
        self.assertNotIn("\u2013", texts)

    def test_edge_case_tickets(self):
        import base64

        vague = self.svc.get_ticket("TCK-1045")
        self.assertFalse(vague["has_logs"])
        self.assertIn("No logs", to_json(self.svc.get_log_stats("TCK-1045")))
        new_issue = self.svc.create_from_example("new-issue")["ticket_id"]
        self.assertEqual(new_issue, "TCK-1047")
        stats = to_json(self.svc.get_log_stats(new_issue))
        self.assertIn("Order total mismatch", stats)
        self.assertIn("tax-engine", stats)
        self.svc.update_ticket_status(new_issue, "Escalated")
        self.assertEqual(self.svc.get_ticket(new_issue)["status"], "Escalated")
        r = self.svc.attach_file(new_issue, "KB-draft.md", base64.b64encode(b"# KB-DRAFT: x").decode())
        self.assertEqual(self.svc.get_file(new_issue, "KB-draft.md")[1], "text/markdown")
        self.assertTrue(r["path"].endswith("KB-draft.md"))

    def test_queue_hides_closed_and_has_no_duplicate_of_the_live_ticket(self):
        visible = [t["ticket_id"] for t in self.svc.console_list()["tickets"]]
        self.assertNotIn("TCK-0877", visible)
        self.assertNotIn("TCK-1046", visible)
        self.assertEqual(self.svc.get_ticket("TCK-0877")["status"], "Closed")

    def test_attach_and_read_file(self):
        import base64

        data = bytes(range(256)) * 600  # ~150 KB, so it spans several chunks
        r = self.svc.attach_file("tck-1042", "errors by hour.png", base64.b64encode(data).decode())
        self.assertEqual(r["path"], "/api/files/TCK-1042/errors_by_hour.png")
        self.assertEqual(self.svc.get_file("TCK-1042", "errors_by_hour.png"), (data, "image/png"))
        self.assertIsNone(self.svc.get_file("TCK-1042", "missing.png"))
        with self.assertRaises(ValueError):
            self.svc.attach_file("TCK-1042", "x.exe", base64.b64encode(b"x").decode())
        with self.assertRaises(ValueError):
            self.svc.attach_file("TCK-1042", "x.png", "not base64!")
        self.svc.reset_demo()
        self.assertIsNone(self.svc.get_file("TCK-1042", "errors_by_hour.png"))

    def test_logs_show_root_cause_and_are_stable(self):
        a = self.svc.get_logs("TCK-1042")
        b = self.svc.get_logs("TCK-1042")
        text = to_json(a)
        self.assertIn("ConnectionPoolExhausted", text)
        self.assertIn("4.2.0", text)
        self.assertEqual(to_json(a), to_json(b))
        self.assertLessEqual(a["log_lines"], 200)

    def test_logs_clamped(self):
        self.assertLessEqual(self.svc.get_logs("TCK-1042", 5)["log_lines"], 20)
        self.assertLessEqual(self.svc.get_logs("TCK-1042", 9999)["log_lines"], 400)

    def test_notes_status_and_customer_update(self):
        self.svc.add_ticket_note("TCK-1042", "Pool exhaustion confirmed", "triage-agent")
        self.svc.update_ticket_status("TCK-1042", "In progress", "Sev2")
        self.svc.send_customer_update("TCK-1042", "Hi Dana - fix: set ORDERSYNC_POOL_SIZE=100.")
        t = self.svc.get_ticket("TCK-1042")
        self.assertEqual(t["status"], "In progress")
        self.assertEqual(t["severity"], "Sev2")
        text = to_json(t)
        self.assertIn("Pool exhaustion confirmed", text)
        self.assertIn("ORDERSYNC_POOL_SIZE=100", text)

    def test_invalid_status_and_severity(self):
        with self.assertRaises(ValueError):
            self.svc.update_ticket_status("TCK-1042", "Done")
        with self.assertRaises(ValueError):
            self.svc.update_ticket_status("TCK-1042", "Resolved", "Sev9")

    def test_empty_note_rejected(self):
        with self.assertRaises(ValueError):
            self.svc.add_ticket_note("TCK-1042", "   ")

    def test_open_tickets_include_followup_target(self):
        waiting = self.svc.list_open_tickets("Awaiting customer")
        ids = [t["ticket_id"] for t in waiting["tickets"]]
        self.assertIn("TCK-1039", ids)

    def test_log_stats_match_raw_logs(self):
        stats = self.svc.get_log_stats("TCK-1042")
        raw = self.svc.get_logs("TCK-1042", 400)["logs"].splitlines()
        self.assertEqual(stats["total_lines"], len(raw))
        self.assertEqual(stats["errors"]["total"], sum(" ERROR " in l for l in raw))
        self.assertEqual(sum(b["errors"] for b in stats["errors_by_hour"]), stats["errors"]["total"])
        self.assertEqual(stats["errors"]["by_type"][0]["type"], "HTTP 503 ServiceUnavailable: ConnectionPoolExhausted")
        self.assertIn("4.2.0", stats["change_event"])
        self.assertLess(stats["change_event"][:20], stats["errors"]["first_at"])
        self.assertIn("ERROR", stats["sample_lines"]["first_error"])
        json.loads(to_json(stats))

    def test_log_stats_without_logs(self):
        self.assertEqual(self.svc.get_log_stats("TCK-1039")["total_lines"], 0)

    def test_reset_restores_state(self):
        self.svc.update_ticket_status("TCK-1042", "Closed")
        self.svc.reset_demo()
        self.assertNotEqual(self.svc.get_ticket("TCK-1042")["status"], "Closed")

    def test_console_example_creates_new_ticket_with_seed_logs(self):
        t = self.svc.create_from_example("known-issue")
        self.assertEqual(t["ticket_id"], "TCK-1047")
        self.assertEqual(t["customer"]["contact_name"], "Dana Whitfield")
        stats = self.svc.get_log_stats("TCK-1047")
        self.assertEqual(stats["errors"]["by_type"][0]["type"], "HTTP 503 ServiceUnavailable: ConnectionPoolExhausted")
        self.assertEqual(self.svc.create_from_example("missing-info")["ticket_id"], "TCK-1048")
        self.assertIn("No logs", to_json(self.svc.get_log_stats("TCK-1048")))
        self.assertIn("TCK-1048", [x["ticket_id"] for x in self.svc.console_list()["tickets"]])
        with self.assertRaises(ValueError):
            self.svc.create_from_example("nope")
        with self.assertRaises(ValueError):
            self.svc.create_ticket("CUST-CONTOSO", "", "x")

    def test_console_automation_state(self):
        tid = "TCK-1042"
        state = lambda: self.svc.console_ticket(tid)
        self.assertFalse(state()["agent_working"])
        self.svc.record_dispatch(tid, f"New ticket {tid}", "case-intake", "disp-1")
        self.assertTrue(state()["agent_working"])
        self.assertTrue(self.svc.console_list()["tickets"][0]["agent_working"])
        self.svc.add_ticket_note(tid, "[Draft awaiting approval]\nDana, set ORDERSYNC_POOL_SIZE=100.", "case-manager")
        s = state()
        self.assertFalse(s["agent_working"])
        self.assertTrue(s["awaiting_approval"])
        self.assertEqual(s["draft"], "Dana, set ORDERSYNC_POOL_SIZE=100.")
        self.svc.record_dispatch(tid, f"approve {tid}", "case-intake", who="support-engineer")
        self.assertTrue(state()["agent_working"])
        self.assertFalse(state()["awaiting_approval"])
        self.svc.send_customer_update(tid, "Dana, set ORDERSYNC_POOL_SIZE=100.")
        s = state()
        self.assertFalse(s["agent_working"] or s["awaiting_approval"])
        self.assertIsNone(s["draft"])

    def test_console_edit_and_request_changes(self):
        tid = "TCK-1042"
        state = lambda: self.svc.console_ticket(tid)
        self.svc.record_dispatch(tid, f"New ticket {tid}", "case-intake")
        self.svc.add_ticket_note(tid, "[Draft awaiting approval]\nDana, set the pool size to 100.", "case-manager")
        self.assertEqual(state()["draft_author"], "case-manager")
        # Ask for changes: the guidance is on the record and the agents are working again.
        self.svc.request_changes(tid, "Also mention that 4.2.1 fixes this.", "Alex Rivera")
        self.svc.record_dispatch(tid, f"revise {tid} by Alex Rivera", "case-intake")
        self.assertTrue(state()["agent_working"])
        self.assertFalse(state()["awaiting_approval"])
        self.assertTrue(any(n["text"] == "[Changes requested] Also mention that 4.2.1 fixes this." for n in state()["notes"]))
        self.svc.add_ticket_note(tid, "[Draft awaiting approval]\nDana, set the pool size to 100, or upgrade to 4.2.1.", "case-manager")
        self.assertEqual(state()["draft"], "Dana, set the pool size to 100, or upgrade to 4.2.1.")
        # Edit, then approve: the engineer's text becomes the draft that gets sent.
        self.svc.save_engineer_edit(tid, "Hi Dana, set the pool size to 100.", "Alex Rivera")
        s = state()
        self.assertTrue(s["awaiting_approval"])
        self.assertEqual((s["draft"], s["draft_author"]), ("Hi Dana, set the pool size to 100.", "Alex Rivera"))
        with self.assertRaises(ValueError):
            self.svc.request_changes(tid, "  ", "Alex Rivera")
        with self.assertRaises(ValueError):
            self.svc.save_engineer_edit(tid, "", "Alex Rivera")

    def test_console_working_times_out_and_deny_clears_draft(self):
        from datetime import timedelta

        from tickets_core import _now

        clock = {"now": _now()}
        svc = TicketService(MemoryStore(), clock=lambda: clock["now"])
        svc.reset_demo()
        svc.add_ticket_note("TCK-1045", "[Draft awaiting approval]\nCould you send logs?", "case-manager")
        svc.record_dispatch("TCK-1045", "deny TCK-1045", "case-intake")
        clock["now"] += timedelta(minutes=11)
        self.assertFalse(svc.console_ticket("TCK-1045")["agent_working"])
        svc.add_ticket_note("TCK-1045", "[Case Manager] The support engineer did not approve the customer draft. Nothing was sent.", "case-manager")
        self.assertFalse(svc.console_ticket("TCK-1045")["awaiting_approval"])

    def _clocked(self):
        from datetime import timedelta

        from tickets_core import _now

        clock = {"now": _now()}
        svc = TicketService(MemoryStore(), clock=lambda: clock["now"])
        svc.reset_demo()

        def advance(**kw):
            clock["now"] += timedelta(**kw)

        return svc, advance

    def test_watchdog_restarts_a_run_that_never_started(self):
        from tickets_core import watchdog_action

        svc, advance = self._clocked()
        tid = "TCK-1042"
        svc.record_dispatch(tid, "New ticket TCK-1042 (run aaa111)", "case-intake")
        notes = lambda: svc.get_ticket(tid)["notes"]
        advance(minutes=2)
        self.assertEqual(svc.watchdog_actions(), [])
        advance(minutes=1)
        [(ticket, action)] = svc.watchdog_actions()
        self.assertEqual((ticket, action[0]), (tid, "restart"))
        self.assertRegex(action[2], r"^New ticket TCK-1042 \(run [0-9a-f]{6}\)$")
        self.assertNotIn("aaa111", action[2])
        svc.record_restart(tid, action[2], "case-intake", action[1])
        s = svc.console_ticket(tid)
        self.assertTrue(s["agent_working"])
        # The old run's late start doesn't count for the new run.
        svc.add_ticket_note(tid, "[Case Manager started] run aaa111 (instance x)", "case-manager")
        advance(minutes=3)
        self.assertEqual(watchdog_action(notes(), svc.clock())[0], "restart")

    def test_watchdog_restarts_stopped_or_idle_runs_then_fails(self):
        svc, advance = self._clocked()
        tid = "TCK-1043"
        svc.record_dispatch(tid, "New ticket TCK-1043 (run bbb222)", "case-intake")
        svc.add_ticket_note(tid, "[Case Manager started] run bbb222 (instance a)", "case-manager")
        advance(minutes=3)
        self.assertEqual(svc.watchdog_actions(), [])  # started and recently active enough
        svc.add_ticket_note(tid, "[Run stopped] run bbb222: Triage could not finish. Foundry kept timing out.", "case-manager")
        [(_, action)] = svc.watchdog_actions()
        self.assertEqual(action[:2], ("restart", "Triage could not finish. Foundry kept timing out."))
        svc.record_restart(tid, action[2], "case-intake", action[1])
        token = action[2].split("(run ")[1].rstrip(")")
        svc.add_ticket_note(tid, f"[Case Manager started] run {token} (instance b)", "case-manager")
        advance(minutes=4)
        [(_, action)] = svc.watchdog_actions()  # quiet for 4 minutes
        self.assertEqual(action[:2], ("restart", "The run stopped making progress."))
        svc.record_restart(tid, action[2], "case-intake", action[1])
        advance(minutes=3)
        [(_, action)] = svc.watchdog_actions()  # two restarts used up
        self.assertEqual(action, ("fail", "Foundry didn't start the run."))
        svc.record_failed(tid, action[1])
        s = svc.console_ticket(tid)
        self.assertFalse(s["agent_working"])
        self.assertTrue(s["run_failed"])
        self.assertIn("didn't start", s["run_error"])
        self.assertTrue(next(t for t in svc.console_list()["tickets"] if t["ticket_id"] == tid)["run_failed"])
        self.assertEqual(svc.watchdog_actions(), [])
        # Retry sends the same request again, and a person's retry gets a fresh set of restarts.
        self.assertEqual(svc.retry_message(tid), "New ticket TCK-1043")
        svc.record_dispatch(tid, "New ticket TCK-1043 (run ccc333)", "case-intake")
        s = svc.console_ticket(tid)
        self.assertTrue(s["agent_working"] and not s["run_failed"])
        advance(minutes=3)
        self.assertEqual(svc.watchdog_actions()[0][1][0], "restart")
        with self.assertRaises(ValueError):
            svc.retry_message(tid)

    def test_watchdog_leaves_finished_runs_alone(self):
        svc, advance = self._clocked()
        tid = "TCK-1042"
        svc.record_dispatch(tid, "New ticket TCK-1042 (run ddd444)", "case-intake")
        svc.add_ticket_note(tid, "[Draft awaiting approval]\nHi Dana.", "case-manager")
        advance(minutes=30)
        self.assertEqual(svc.watchdog_actions(), [])
        s = svc.console_ticket(tid)
        self.assertTrue(s["awaiting_approval"])
        self.assertFalse(s["run_failed"])
        # An approval that never finished is retried as the same approval.
        svc.record_dispatch(tid, "approve TCK-1042 by Alex Rivera (run eee555)", "case-intake")
        advance(minutes=11)
        s = svc.console_ticket(tid)
        self.assertTrue(s["run_failed"])
        self.assertTrue(s["awaiting_approval"])
        self.assertEqual(svc.watchdog_actions()[0][1][0], "fail")  # the backstop: on record as failed
        self.assertEqual(svc.retry_message(tid), "approve TCK-1042 by Alex Rivera")


if __name__ == "__main__":
    unittest.main()
