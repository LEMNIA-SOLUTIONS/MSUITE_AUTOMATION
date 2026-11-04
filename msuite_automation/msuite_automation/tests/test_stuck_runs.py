import json
from unittest.mock import patch

import frappe
from frappe.tests import IntegrationTestCase
from frappe.utils import add_to_date, now_datetime

from msuite_automation.msuite_automation.constants import RUN_DOCTYPE
from msuite_automation.msuite_automation.engine import executor

TITLE = "ZZ stuck test"


def make(steps=None, edges=None, triggers=None, disabled=0, **kw):
	doc = frappe.get_doc({
		"doctype": "Messaging Automation",
		"title": TITLE,
		"disabled": 1,
		"triggers": triggers or [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
		"steps": steps or [{"step_id": "s1", "step_type": "Send SMS", "sms_message": "hi"}],
		"edges": [{"source_id": a, "source_handle": h, "target_id": b} for a, h, b in (edges or [("t1", "", "s1")])],
		**kw,
	})
	doc.flags.ignore_plan_gate = True
	doc.insert(ignore_permissions=True, ignore_links=True)
	if not disabled:
		frappe.db.set_value("Messaging Automation", doc.name, "disabled", 0)
	return doc.name


def todo(desc="ZZ stuck test", **kw):
	return frappe.get_doc({"doctype": "ToDo", "description": desc, **kw}).insert(ignore_permissions=True).name


def make_run(automation_name, ref_name, status="Running", paths=None):
	run = frappe.get_doc({
		"doctype": RUN_DOCTYPE,
		"automation": automation_name,
		"trigger_id": "t1",
		"status": status,
		"reference_doctype": "ToDo",
		"reference_name": ref_name,
		"context": json.dumps({"doc_dict": None}),
		"log": json.dumps([{"path_id": "p1", "node_id": "t1", "step_id": "t1", "step_type": "Trigger", "result": "started", "at": str(now_datetime())}]),
		"paths": paths or [{"path_id": "p1", "node_id": "s1", "status": status, "executed": 0, "joined": 0}],
	})
	run.flags.ignore_links = True
	run.insert(ignore_permissions=True)
	return run


def log_of(run_name):
	return json.loads(frappe.db.get_value(RUN_DOCTYPE, run_name, "log") or "[]")


class TestStuckRuns(IntegrationTestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": TITLE}, pluck="name"):
			run_names = frappe.get_all(RUN_DOCTYPE, filters={"automation": name}, pluck="name")
			if run_names:
				frappe.db.sql("delete from `tabAutomation Run Path` where parent in %s", [tuple(run_names)])
				frappe.db.delete(RUN_DOCTYPE, {"name": ("in", run_names)})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "ZZ stuck%")})

	def test_stuck_running_run_is_failed(self):
		auto = make()
		ref = todo()
		run = make_run(auto, ref, status="Running", paths=[
			{"path_id": "p1", "node_id": "s1", "status": "Running", "executed": 0, "joined": 0},
			{"path_id": "p2", "node_id": "s2", "status": "Waiting", "executed": 0, "joined": 0, "resume_at": now_datetime()},
		])
		past = add_to_date(now_datetime(), minutes=-31)
		frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name=%s", (past, run.name))

		executor.sweep_stuck_runs()

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run.name, "status"), "Failed")
		log = log_of(run.name)
		self.assertTrue(any(e.get("result") == "error" and e.get("step_id") == "worker lost" for e in log))
		path_statuses = frappe.db.sql_list("select status from `tabAutomation Run Path` where parent=%s", run.name)
		self.assertTrue(path_statuses and all(s == "Ended" for s in path_statuses))

	def test_recent_running_run_untouched(self):
		auto = make()
		ref = todo()
		run = make_run(auto, ref, status="Running", paths=[
			{"path_id": "p1", "node_id": "s1", "status": "Running", "executed": 0, "joined": 0},
		])
		recent = add_to_date(now_datetime(), minutes=-5)
		frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name=%s", (recent, run.name))

		executor.sweep_stuck_runs()

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run.name, "status"), "Running")
		log = log_of(run.name)
		self.assertFalse(any(e.get("step_id") == "worker lost" for e in log))
		path_statuses = frappe.db.sql_list("select status from `tabAutomation Run Path` where parent=%s", run.name)
		self.assertEqual(path_statuses, ["Running"])

	def test_waiting_run_untouched(self):
		auto = make()
		ref = todo()
		run = make_run(auto, ref, status="Waiting", paths=[
			{"path_id": "p1", "node_id": "s1", "status": "Waiting", "executed": 0, "joined": 0, "resume_at": add_to_date(now_datetime(), hours=1)},
		])
		past = add_to_date(now_datetime(), minutes=-35)
		frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name=%s", (past, run.name))

		executor.sweep_stuck_runs()

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run.name, "status"), "Waiting")
		path_statuses = frappe.db.sql_list("select status from `tabAutomation Run Path` where parent=%s", run.name)
		self.assertEqual(path_statuses, ["Waiting"])

	def test_run_completed_between_select_and_update_stays_completed(self):
		auto = make()
		ref = todo()
		run = make_run(auto, ref, status="Running", paths=[
			{"path_id": "p1", "node_id": "s1", "status": "Running", "executed": 0, "joined": 0},
		])
		past = add_to_date(now_datetime(), minutes=-35)
		frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name=%s", (past, run.name))

		orig_sql = frappe.db.sql
		def race_sql(query, *args, **kwargs):
			if isinstance(query, str) and query.strip().startswith("update `tabMessaging Automation Run` set status='Failed'"):
				orig_sql(f"update `tab{RUN_DOCTYPE}` set status='Completed' where name=%s", run.name)
			return orig_sql(query, *args, **kwargs)

		with patch("frappe.db.sql", side_effect=race_sql):
			executor.sweep_stuck_runs()

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run.name, "status"), "Completed")
		log = log_of(run.name)
		self.assertFalse(any(e.get("step_id") == "worker lost" for e in log))

	def test_resume_waiting_runs_claims_with_fresh_modified(self):
		auto = make()
		ref = todo()
		run = make_run(auto, ref, status="Waiting", paths=[
			{"path_id": "p1", "node_id": "s1", "status": "Waiting", "executed": 0, "joined": 0, "resume_at": add_to_date(now_datetime(), minutes=-5)},
		])
		past = add_to_date(now_datetime(), hours=-2)
		frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name=%s", (past, run.name))

		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run.name]), patch.object(executor, "_walk"):
			executor.resume_waiting_runs()

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run.name, "status"), "Running")
		modified = frappe.db.get_value(RUN_DOCTYPE, run.name, "modified")
		self.assertGreater(modified, add_to_date(now_datetime(), minutes=-1))

	def test_sweep_stuck_runs_per_row_commit_survives_failure(self):
		auto = make()
		ref1 = todo(desc="ZZ stuck test 1")
		ref2 = todo(desc="ZZ stuck test 2")
		run1 = make_run(auto, ref1, status="Running", paths=[
			{"path_id": "p1", "node_id": "s1", "status": "Running", "executed": 0, "joined": 0},
		])
		run2 = make_run(auto, ref2, status="Running", paths=[
			{"path_id": "p1", "node_id": "s1", "status": "Running", "executed": 0, "joined": 0},
		])
		past = add_to_date(now_datetime(), minutes=-35)
		frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name in (%s, %s)", (past, run1.name, run2.name))
		frappe.db.commit()  # the sweeper rolls back on run1's failure; without this it would erase run2 too, depending on name order

		orig_mark_failed = executor._mark_failed

		def faulty_mark_failed(name, where):
			if name == run1.name:
				raise RuntimeError("Simulated failure for run1")
			return orig_mark_failed(name, where)

		with patch.object(executor, "_mark_failed", side_effect=faulty_mark_failed):
			executor.sweep_stuck_runs()

		# run2 is successfully failed and committed despite run1 raising
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run2.name, "status"), "Failed")
		log2 = log_of(run2.name)
		self.assertTrue(any(e.get("result") == "error" and e.get("step_id") == "worker lost" for e in log2))

	def test_sweeper_cutoff_is_above_the_longest_job_timeout(self):
		"""A live job must never look stuck: the cutoff has to outlast the longest RQ job."""
		self.assertGreater(executor.STUCK_AFTER_MINUTES * 60, executor.JOB_TIMEOUT)
