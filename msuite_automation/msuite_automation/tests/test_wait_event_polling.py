import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from msuite_automation.msuite_automation.constants import RUN_DOCTYPE, WAIT_EVENT
from msuite_automation.msuite_automation.engine import executor
from msuite_automation.msuite_automation.engine.channels import SendRetry

CH = "msuite_automation.msuite_automation.engine.channels"
TITLE = "ZZ wait event polling test"


def make(steps, edges, triggers=None, disabled=0, **kw):
	doc = frappe.get_doc({
		"doctype": "Messaging Automation",
		"title": TITLE,
		"disabled": 1,
		"triggers": triggers or [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
		"steps": steps,
		"edges": [{"source_id": a, "source_handle": h, "target_id": b} for a, h, b in edges],
		**kw,
	})
	doc.flags.ignore_plan_gate = True
	doc.insert(ignore_permissions=True, ignore_links=True)
	if not disabled:
		frappe.db.set_value("Messaging Automation", doc.name, "disabled", 0)
	return doc.name


def todo(desc="ZZ wait event polling test", **kw):
	return frappe.get_doc({"doctype": "ToDo", "description": desc, **kw}).insert(ignore_permissions=True).name


def sms(sid, msg="hi"):
	return {"step_id": sid, "step_type": "Send SMS", "sms_message": msg}


def log_of(run):
	return [(e["step_id"], e["result"]) for e in json.loads(frappe.db.get_value(RUN_DOCTYPE, run, "log"))]


@patch(f"{CH}.send_whatsapp", return_value=True)
@patch(f"{CH}.send_email", return_value=True)
@patch(f"{CH}.send_sms", return_value=True)
class TestWaitEventPolling(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": TITLE}, pluck="name"):
			frappe.db.delete(RUN_DOCTYPE, {"automation": name})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "ZZ wait event polling%")})

	def test_not_met_event_run_no_running_update_and_at_most_one_commit(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 2, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])
		ref = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertIsNotNone(run)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")

		past = add_to_date(now_datetime(), minutes=-15)
		frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name=%s", (past, run))

		orig_sql = frappe.db.sql
		running_updates = []

		def tracking_sql(query, *args, **kwargs):
			q_str = str(query).lower()
			if "status='running'" in q_str:
				running_updates.append(query)
			return orig_sql(query, *args, **kwargs)

		commit_count = 0
		orig_commit = frappe.db.commit

		def tracking_commit():
			nonlocal commit_count
			commit_count += 1
			return orig_commit()

		with patch("frappe.db.sql", side_effect=tracking_sql), patch("frappe.db.commit", side_effect=tracking_commit):
			executor.poll_event_waits()

		# Run remains Waiting, was never updated to Running, commit called at most once
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")
		self.assertEqual(len(running_updates), 0)
		self.assertLessEqual(commit_count, 1)
		self.assertEqual(sms_.call_count, 0)

		# Modified timestamp was touched by the batched update
		new_mod = frappe.db.get_value(RUN_DOCTYPE, run, "modified")
		self.assertGreater(new_mod, past)

	def test_met_run_claimed_and_resumed(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 2, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])
		ref = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")

		# Condition becomes met in database
		frappe.db.set_value("ToDo", ref, "status", "Closed")

		executor.poll_event_waits()

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		steps = [s for s, _ in log_of(run)]
		self.assertIn("on_event", steps)
		self.assertNotIn("on_timeout", steps)
		self.assertGreaterEqual(sms_.call_count, 1)

	def test_timeout_run_resumed_with_timeout_handle(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 2, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])
		ref = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")

		# Timeout reached in past, condition NOT met
		past = add_to_date(now_datetime(), minutes=-10)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.set_value(RUN_DOCTYPE, run, "resume_at", past)

		executor.resume_waiting_runs()

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		steps = [s for s, _ in log_of(run)]
		self.assertIn("on_timeout", steps)
		self.assertNotIn("on_event", steps)
		self.assertGreaterEqual(sms_.call_count, 1)

	def test_consecutive_ticks_check_different_runs(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 5, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])

		runs = []
		for i in range(4):
			ref = todo(status="Open", desc=f"ZZ wait event polling batch {i}")
			runs.append(executor.run_automation(name, "t1", "ToDo", ref))

		# Set modified timestamps explicitly: run 0 & 1 older, run 2 & 3 newer
		now = now_datetime()
		older = add_to_date(now, minutes=-30)
		newer = add_to_date(now, minutes=-10)
		frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name in (%s, %s)", (older, runs[0], runs[1]))
		frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name in (%s, %s)", (newer, runs[2], runs[3]))

		with patch.object(executor, "DUE_BATCH", 2):
			# Tick 1: picks the 2 oldest runs
			tick1_due = executor._event_run_names()
			self.assertEqual(len(tick1_due), 2)
			self.assertEqual(set(tick1_due), {runs[0], runs[1]})

			executor.poll_event_waits()

			# Tick 2: runs 0 & 1 were touched and have fresh modified, so tick 2 picks runs 2 & 3
			tick2_due = executor._event_run_names()
			self.assertEqual(len(tick2_due), 2)
			self.assertEqual(set(tick2_due), {runs[2], runs[3]})
			self.assertTrue(set(tick1_due).isdisjoint(set(tick2_due)))

	def test_retry_parked_run_unaffected(self, sms_, *_):
		name = make([
			{"step_id": "s1", "step_type": "Send SMS", "sms_message": "test retry"},
		], [("t1", "", "s1")])
		ref = todo(status="Open")

		with patch(f"{CH}.send_sms", side_effect=SendRetry("transient", "network timeout")):
			run = executor.run_automation(name, "t1", "ToDo", ref)

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")
		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["retry_count", "wait_for_event"])
		self.assertEqual(paths[0].retry_count, 1)
		self.assertEqual(paths[0].wait_for_event, 0)

		# Make retry delay expire
		past = add_to_date(now_datetime(), minutes=-5)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.set_value(RUN_DOCTYPE, run, "resume_at", past)

		# Retry run resumes, claims, and succeeds
		executor.resume_waiting_runs()
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		steps = [s for s, _ in log_of(run)]
		self.assertIn("s1", steps)
