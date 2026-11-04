import inspect
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_to_date, now_datetime

from msuite_automation.msuite_automation import constants as C
from msuite_automation.msuite_automation.api import automations as api
from msuite_automation.msuite_automation.constants import RUN_DOCTYPE
from msuite_automation.msuite_automation.engine import channels, event_check, executor, preview, triggers, walker

CH = "msuite_automation.msuite_automation.engine.channels"
EX = "msuite_automation.msuite_automation.engine.executor"
TITLE = "ZZ constants test"
JSON_PATH = os.path.join(os.path.dirname(__file__), "..", "doctype", "automation_run_path", "automation_run_path.json")


def make(steps, edges, **kw):
	doc = frappe.get_doc({"doctype": "Messaging Automation", "title": TITLE, "disabled": 1,
		"triggers": [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
		"steps": steps, "edges": [{"source_id": a, "source_handle": h, "target_id": b} for a, h, b in edges], **kw})
	doc.flags.ignore_plan_gate = True
	doc.insert(ignore_permissions=True, ignore_links=True)
	frappe.db.set_value("Messaging Automation", doc.name, "disabled", 0)
	return doc.name


def todo():
	return frappe.get_doc({"doctype": "ToDo", "description": "ZZ constants test"}).insert(ignore_permissions=True).name


def sms(sid):
	return {"step_id": sid, "step_type": "Send SMS", "sms_message": "hi"}


class TestSingleSources(unittest.TestCase):
	def test_short_queue_is_defined_once(self):
		self.assertIs(executor.QUEUE_SHORT, C.QUEUE_SHORT)
		self.assertIs(triggers.QUEUE_SHORT, C.QUEUE_SHORT)
		self.assertEqual(C.QUEUE_SHORT, "short")

	def test_walker_default_step_limit(self):
		self.assertEqual(inspect.signature(walker.advance).parameters["limit"].default, C.WALK_STEP_LIMIT)

	def test_acquire_lock_default_ttl(self):
		cache = MagicMock()
		cache.make_key.side_effect = lambda k: k
		cache.set.return_value = True
		with patch(f"{CH}.frappe.cache", return_value=cache):
			channels.acquire_lock("k")
		self.assertEqual(cache.set.call_args.kwargs["ex"], C.DEDUPE_LOCK_TTL_SECONDS)

	def test_wait_key_json_length_matches_constant(self):
		with open(JSON_PATH) as f:
			field = next(x for x in json.load(f)["fields"] if x["fieldname"] == "wait_key")
		self.assertEqual(field["length"], C.WAIT_KEY_MAX_LENGTH)

	def test_values_unchanged(self):
		self.assertEqual((C.RUN_CLAIM_TTL_SECONDS, C.STUCK_SWEEP_BATCH, C.WAIT_KEY_MAX_LENGTH, C.EVENT_CANDIDATE_LIMIT, C.WALK_STEP_LIMIT,
			C.DEDUPE_LOCK_TTL_SECONDS, C.RETRY_NOTE_MAX, C.RUN_ERROR_MAX, C.PREVIEW_ERROR_MAX, C.RUN_LIST_DEFAULT_LIMIT, C.RUN_LIST_MAX_LIMIT),
			(900, 200, 140, 100, 500, 86400, 200, 500, 300, 10, 50))


class TestEventCheckLimits(unittest.TestCase):
	def _step(self):
		return frappe._dict(check_doctype="ToDo", check_link_field="name", check_match_field="description")

	def test_wait_key_length_boundary(self):
		n = C.WAIT_KEY_MAX_LENGTH
		self.assertEqual(event_check.wait_key(self._step(), frappe._dict(description="x" * n)), "x" * n)
		self.assertEqual(event_check.wait_key(self._step(), frappe._dict(description="x" * (n + 1))), "")
		self.assertEqual(event_check.wait_key(self._step(), frappe._dict(description="😀" * n)), "😀" * n)

	def test_candidate_lookup_uses_the_limit(self):
		step = frappe._dict(check_doctype="ToDo", check_link_field="name", check_match_field="description",
			condition_rules=json.dumps([{"field": "status", "operator": "=", "value": "Open"}]))
		with patch.object(event_check.frappe, "get_all", return_value=[]) as get_all:
			event_check.event_met(step, frappe._dict(doctype="ToDo", name="ZZ-none", description="abc"))
		self.assertEqual(get_all.call_args.kwargs["limit"], C.EVENT_CANDIDATE_LIMIT)


class TestRunListLimit(unittest.TestCase):
	def test_clamping(self):
		cases = [(0, C.RUN_LIST_DEFAULT_LIMIT), (-5, 1), ("abc", C.RUN_LIST_DEFAULT_LIMIT), (None, C.RUN_LIST_DEFAULT_LIMIT),
			(C.RUN_LIST_MAX_LIMIT + 1, C.RUN_LIST_MAX_LIMIT), (1000, C.RUN_LIST_MAX_LIMIT), (7, 7), (1, 1), (C.RUN_LIST_MAX_LIMIT, C.RUN_LIST_MAX_LIMIT)]
		for given, expected in cases:
			with self.subTest(limit=given), patch.object(api, "check_read"), patch.object(api.frappe, "get_all", return_value=[]) as get_all:
				api.get_runs("A", given)
				self.assertEqual(get_all.call_args.kwargs["limit"], expected)


class TestErrorTextLimits(unittest.TestCase):
	def test_mark_failed_error_cut(self):
		fixed_now = now_datetime()
		for size in (C.RUN_ERROR_MAX - 1, C.RUN_ERROR_MAX, C.RUN_ERROR_MAX + 1):
			with self.subTest(size=size), patch("frappe.get_traceback", return_value="é" * size), \
				patch("frappe.db.get_value", return_value="[]"), patch("frappe.db.set_value") as set_value, patch("frappe.db.sql"), \
				patch(f"{EX}.now_datetime", return_value=fixed_now):
				executor._mark_failed("RUN", "executor")
				log = json.loads(set_value.call_args.args[2]["log"])
				self.assertEqual(len(log[-1]["error"]), min(size, C.RUN_ERROR_MAX))

	def test_preview_filter_error_cut(self):
		trigger = frappe._dict(trigger_id="t1", filter_logic="All", condition=None, trigger_type="Document Event")
		automation = frappe._dict(filters=[], skip_return_documents=0)
		with patch.object(preview, "_passes", side_effect=ValueError("é" * (C.PREVIEW_ERROR_MAX + 5))):
			out = preview._filters(automation, trigger, frappe._dict())
		self.assertEqual(len(out["error"]), C.PREVIEW_ERROR_MAX)


class TestExecutorLimits(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": TITLE}, pluck="name"):
			frappe.db.delete(RUN_DOCTYPE, {"automation": name})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "ZZ constants%")})

	def _run_with_step_error(self, error):
		name = make([sms("s")], [("t1", "", "s")])
		with patch(f"{EX}.S.execute", side_effect=error), patch(f"{EX}.frappe.log_error"):
			run = executor.run_automation(name, "t1", "ToDo", todo())
		return json.loads(frappe.db.get_value(RUN_DOCTYPE, run, "log"))[-1]

	def test_step_error_text_cut(self):
		entry = self._run_with_step_error(ValueError("é" * (C.RUN_ERROR_MAX + 5)))
		self.assertEqual(len(entry["error"]), C.RUN_ERROR_MAX)

	def test_retry_note_cut(self):
		entry = self._run_with_step_error(channels.SendRetry("transient", "é" * (C.RETRY_NOTE_MAX + 5)))
		self.assertEqual(entry["result"], "retrying (1/3): " + "é" * C.RETRY_NOTE_MAX)

	def test_claim_ttl_is_passed_to_the_lock(self):
		name = make([sms("s")], [("t1", "", "s")], send_frequency="Once per document")
		with patch(f"{CH}.acquire_lock", return_value=False) as lock, patch.object(executor, "RUN_CLAIM_TTL_SECONDS", 123):
			executor.run_automation(name, "t1", "ToDo", todo())
		self.assertEqual(lock.call_args.kwargs["ttl"], 123)

	def test_sweeper_batch(self):
		name = make([sms("s")], [("t1", "", "s")])
		past = add_to_date(now_datetime(), minutes=-(executor.STUCK_AFTER_MINUTES + 5))
		for _ in range(3):
			run = frappe.get_doc({"doctype": RUN_DOCTYPE, "automation": name, "trigger_id": "t1", "status": "Running",
				"reference_doctype": "ToDo", "reference_name": todo(), "context": "{}", "log": "[]",
				"paths": [{"path_id": "p1", "node_id": "s", "status": "Running", "executed": 0, "joined": 0}]})
			run.flags.ignore_links = True
			run.insert(ignore_permissions=True)
			frappe.db.sql(f"update `tab{RUN_DOCTYPE}` set modified=%s where name=%s", (past, run.name))
		with patch.object(executor, "STUCK_SWEEP_BATCH", 1):
			executor.sweep_stuck_runs()
		self.assertEqual(frappe.db.count(RUN_DOCTYPE, {"automation": name, "status": "Failed"}), 1)
