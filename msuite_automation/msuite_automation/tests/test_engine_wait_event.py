import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from msuite_automation.msuite_automation.constants import RUN_DOCTYPE, WAIT_EVENT
from msuite_automation.msuite_automation.engine import executor

CH = "msuite_automation.msuite_automation.engine.channels"
TITLE = "ZZ wait event test"


def make(steps, edges, triggers=None, disabled=0, **kw):
	doc = frappe.get_doc({"doctype": "Messaging Automation", "title": TITLE, "disabled": 1,
		"triggers": triggers or [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
		"steps": steps, "edges": [{"source_id": a, "source_handle": h, "target_id": b} for a, h, b in edges], **kw})
	doc.flags.ignore_plan_gate = True
	doc.insert(ignore_permissions=True, ignore_links=True)
	if not disabled:
		frappe.db.set_value("Messaging Automation", doc.name, "disabled", 0)
	return doc.name


def todo(desc="ZZ wait event test", **kw):
	return frappe.get_doc({"doctype": "ToDo", "description": desc, **kw}).insert(ignore_permissions=True).name


def sms(sid, msg="hi"):
	return {"step_id": sid, "step_type": "Send SMS", "sms_message": msg}


def log_of(run):
	return [(e["step_id"], e["result"]) for e in json.loads(frappe.db.get_value(RUN_DOCTYPE, run, "log"))]


@patch(f"{CH}.send_whatsapp", return_value=True)
@patch(f"{CH}.send_email", return_value=True)
@patch(f"{CH}.send_sms", return_value=True)
class TestEngineWaitEvent(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": TITLE}, pluck="name"):
			frappe.db.delete(RUN_DOCTYPE, {"automation": name})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "ZZ wait event%")})

	def test_already_true_takes_event_immediately(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 1, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Open"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])
		ref = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertIsNotNone(run)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		steps = [s for s, _ in log_of(run)]
		self.assertIn("on_event", steps)
		self.assertNotIn("on_timeout", steps)

	def test_false_parks_with_resume_at_and_marker(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 2, "wait_unit": "Hours",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])
		ref = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertIsNotNone(run)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")
		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["status", "wait_for_event", "resume_at"])
		self.assertEqual(len(paths), 1)
		self.assertEqual(paths[0].status, "Waiting")
		self.assertEqual(paths[0].wait_for_event, 1)
		self.assertIsNotNone(paths[0].resume_at)
		self.assertEqual(sms_.call_count, 0)

	def test_event_run_names_includes_wait_for_event_with_future_timeout(self, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 5, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event"), sms("on_timeout"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])
		ref = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertNotIn(run, executor._due_run_names())
		self.assertIn(run, executor._event_run_names())

	def test_event_beats_timeout_in_one_tick(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 1, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])
		ref = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")
		# Condition met in DB
		frappe.db.set_value("ToDo", ref, "status", "Closed")
		# Timeout also reached in the past
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (add_to_date(now_datetime(), minutes=-5), run))
		frappe.db.set_value(RUN_DOCTYPE, run, "resume_at", add_to_date(now_datetime(), minutes=-5))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		steps = [s for s, _ in log_of(run)]
		self.assertIn("on_event", steps)
		self.assertNotIn("on_timeout", steps)

	def test_timeout_takes_timeout_handle(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 1, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])
		ref = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", ref)
		# Condition NOT met (still Open)
		# Timeout reached in the past
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (add_to_date(now_datetime(), minutes=-5), run))
		frappe.db.set_value(RUN_DOCTYPE, run, "resume_at", add_to_date(now_datetime(), minutes=-5))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		steps = [s for s, _ in log_of(run)]
		self.assertIn("on_timeout", steps)
		self.assertNotIn("on_event", steps)

	def test_still_waiting_leaves_run_waiting_without_walking(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 1, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])
		ref = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", ref)
		# Condition NOT met, timeout in the future
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")
		self.assertEqual(sms_.call_count, 0)

	def test_deleted_document_skips_event_waiting_run(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 1, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
		], [("t1", "", "we"), ("we", "event", "on_event")])
		ref = todo(status="Open")
		doc_dict = frappe.get_doc("ToDo", ref).as_dict()
		run = executor.run_automation(name, "t1", "ToDo", ref, doc_dict=doc_dict)
		frappe.delete_doc("ToDo", ref, force=True, ignore_permissions=True)
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Skipped")
		self.assertEqual(sms_.call_count, 0)

	def test_fresh_read_not_doc_dict_on_poll(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 1, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])
		ref = todo(status="Open")
		# Run started with doc_dict where status is "Open"
		run = executor.run_automation(name, "t1", "ToDo", ref, doc_dict={"doctype": "ToDo", "name": ref, "status": "Open", "description": "ZZ wait event test"})
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")
		# Update status in database to "Closed"
		frappe.db.set_value("ToDo", ref, "status", "Closed")
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		steps = [s for s, _ in log_of(run)]
		self.assertIn("on_event", steps)

	def test_atomic_claim_used(self, sms_, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 1, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
		], [("t1", "", "we"), ("we", "event", "on_event")])
		ref = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", ref)
		# Another worker claimed it first by flipping to Running
		frappe.db.set_value(RUN_DOCTYPE, run, "status", "Running")
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		# It should not have been processed by resume_waiting_runs, sms should not be called
		self.assertEqual(sms_.call_count, 0)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Running")

