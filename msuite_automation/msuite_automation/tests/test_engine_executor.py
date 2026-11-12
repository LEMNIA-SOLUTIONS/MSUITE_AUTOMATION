import json
import unittest
from unittest.mock import patch

import frappe
from frappe.model.document import Document
from frappe.utils import add_to_date, now_datetime

from msuite_automation.msuite_automation.constants import RUN_DOCTYPE
from msuite_automation.msuite_automation.engine import channels, executor

CH = "msuite_automation.msuite_automation.engine.channels"
EX = "msuite_automation.msuite_automation.engine.executor"
TITLE = "ZZ exec test"


def make(steps, edges, triggers=None, disabled=0, **kw):
	doc = frappe.get_doc({"doctype": "Messaging Automation", "title": TITLE, "disabled": 1,
		"triggers": triggers or [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
		"steps": steps, "edges": [{"source_id": a, "source_handle": h, "target_id": b} for a, h, b in edges], **kw})
	doc.flags.ignore_plan_gate = True
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True, ignore_links=True)
	if not disabled:
		frappe.db.set_value("Messaging Automation", doc.name, "disabled", 0)
	return doc.name


def todo(desc="ZZ exec test", **kw):
	return frappe.get_doc({"doctype": "ToDo", "description": desc, **kw}).insert(ignore_permissions=True).name


def sms(sid, msg="hi"):
	return {"step_id": sid, "step_type": "Send SMS", "sms_message": msg}


def log_of(run):
	return [(e["step_id"], e["result"]) for e in json.loads(frappe.db.get_value(RUN_DOCTYPE, run, "log"))]


@patch(f"{CH}.send_whatsapp", return_value=True)
@patch(f"{CH}.send_email", return_value=True)
@patch(f"{CH}.send_sms", return_value=True)
class TestGraphRun(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": TITLE}, pluck="name"):
			frappe.db.delete(RUN_DOCTYPE, {"automation": name})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "ZZ exec%")})

	def test_if_else_and_switch(self, *_):
		name = make([{"step_id": "i", "step_type": "If/Else", "condition_rules": json.dumps([{"field": "priority", "operator": "=", "value": "High"}])},
			{"step_id": "sw", "step_type": "Switch", "switch_field": "status", "switch_cases": json.dumps(["Open", "Closed"])},
			sms("hi_open"), sms("other"), sms("low")],
			[("t1", "", "i"), ("i", "yes", "sw"), ("i", "no", "low"), ("sw", "case_1", "hi_open"), ("sw", "otherwise", "other")])
		run = executor.run_automation(name, "t1", "ToDo", todo(priority="High", status="Open"))
		self.assertEqual([s for s, _ in log_of(run)], ["t1", "i", "sw", "hi_open"])
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")

	def test_fan_out_and_join_run_once(self, sms_, *_):
		name = make([sms("a") | {"pos_x": 0}, sms("b") | {"pos_x": 200}, sms("j")],
			[("t1", "", "a"), ("t1", "", "b"), ("a", "", "j"), ("b", "", "j")])
		run = executor.run_automation(name, "t1", "ToDo", todo())
		self.assertEqual([s for s, _ in log_of(run)], ["t1", "a", "b", "j"])
		self.assertEqual(sms_.call_count, 3)

	def test_join_after_wait_resumes_once(self, sms_, *_):
		name = make([sms("a") | {"pos_x": 0}, {"step_id": "w", "step_type": "Wait", "wait_value": 1, "wait_unit": "Days", "pos_x": 200}, sms("j")],
			[("t1", "", "a"), ("t1", "", "w"), ("a", "", "j"), ("w", "", "j")])
		run = executor.run_automation(name, "t1", "ToDo", todo())
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")
		self.assertEqual(sms_.call_count, 1)  # only "a"; join waits for the waiting path
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s and status='Waiting'", (add_to_date(now_datetime(), minutes=-1), run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		self.assertEqual(sms_.call_count, 2)
		self.assertEqual([s for s, _ in log_of(run)].count("j"), 1)

	def test_mixed_doctypes_use_their_own_mapping_and_recipient(self, sms_, *_):
		triggers = [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"},
			{"trigger_id": "t2", "trigger_type": "Document Event", "reference_doctype": "Note", "doc_event": "After Save"}]
		name = make([{"step_id": "s", "step_type": "Send SMS", "sms_message": "x", "send_to": json.dumps({"ToDo": "description"})}],
			[("t1", "", "s"), ("t2", "", "s")], triggers=triggers,
			recipients=[{"reference_doctype": "Note", "phone_field": "title"}])
		a = frappe.get_doc("Messaging Automation", name)
		step = a.steps[0]
		self.assertEqual(executor.effective_automation(a, step, "ToDo").phone_field, "description")
		self.assertEqual(executor.effective_automation(a, step, "Note").phone_field, "title")

	def test_effective_mappings_and_accounts(self, *_):
		a = frappe._dict(name="MA", send_frequency="Every time", skip_return_documents=0, whatsapp_account="acc", sms_account="",
			phone_field="", email_field="", recipients=[],
			variable_mappings=[frappe._dict(step_id="w", reference_doctype="ToDo", variable_key="1"), frappe._dict(step_id="w", reference_doctype="Note", variable_key="1"),
				frappe._dict(step_id="w", reference_doctype="", variable_key="2")])
		step = frappe._dict(step_id="w", step_type="Send WhatsApp", send_to=None, whatsapp_account="", sms_account="")
		eff = executor.effective_automation(a, step, "ToDo")
		self.assertEqual(sorted((m.reference_doctype, m.variable_key) for m in eff.variable_mappings), [("", "2"), ("ToDo", "1")])
		self.assertEqual(eff.whatsapp_account, "acc")
		step.whatsapp_account = "other"
		self.assertEqual(executor.effective_automation(a, step, "ToDo").whatsapp_account, "other")
		self.assertEqual(eff.whatsapp_phone, "")
		step.whatsapp_phone = "P2"
		self.assertEqual(executor.effective_automation(a, step, "ToDo").whatsapp_phone, "P2")

	def test_trigger_filters_are_per_trigger(self, sms_, *_):
		name = make([sms("s")], [("t1", "", "s")], filters=[{"trigger_id": "t1", "field": "priority", "operator": "=", "value": "High"}])
		self.assertIsNone(executor.run_automation(name, "t1", "ToDo", todo(priority="Low")))
		self.assertTrue(executor.run_automation(name, "t1", "ToDo", todo(priority="High")))

	def test_legacy_waiting_run_resumes(self, sms_, *_):
		name = make([sms("a"), sms("b")], [("t1", "", "a"), ("a", "", "b")])
		ref = todo()
		run = frappe.get_doc({"doctype": RUN_DOCTYPE, "automation": name, "trigger_id": "t1", "status": "Waiting",
			"reference_doctype": "ToDo", "reference_name": ref, "resume_step_id": "b",
			"resume_at": add_to_date(now_datetime(), minutes=-1), "log": "[]", "context": "{}"}).insert(ignore_permissions=True, ignore_links=True)
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run.name]):
			executor.resume_waiting_runs()
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run.name, "status"), "Completed")
		self.assertEqual([s for s, _ in log_of(run.name)], ["b"])

	def test_once_per_document(self, sms_, *_):
		name = make([sms("s")], [("t1", "", "s")], send_frequency="Once per document")
		ref = todo()
		self.assertTrue(executor.run_automation(name, "t1", "ToDo", ref))
		self.assertIsNone(executor.run_automation(name, "t1", "ToDo", ref))

	def test_failing_channel_does_not_stop_others(self, sms_, email, wa):
		email.side_effect = RuntimeError("smtp down")
		name = make([{"step_id": "e", "step_type": "Send Email", "email_subject": "x"}, sms("s")], [("t1", "", "e"), ("e", "", "s")])
		run = executor.run_automation(name, "t1", "ToDo", todo())
		self.assertEqual(log_of(run)[1][1], "error")
		self.assertEqual(log_of(run)[2][1], "sent")
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "sent_count"), 1)

	def test_run_automation_marks_failed_on_exception(self, *_):
		name = make([sms("s")], [("t1", "", "s")])
		with patch.object(executor, "advance", side_effect=RuntimeError("boom")):
			executor.run_automation(name, "t1", "ToDo", todo())
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, {"automation": name}, "status"), "Failed")

	@patch(f"{CH}.acquire_lock", side_effect=channels.LockUnavailable("redis down"))
	def test_run_claim_lock_unavailable_skips_run_and_logs_warning(self, mock_lock, *_):
		name = make([sms("s")], [("t1", "", "s")], send_frequency="Once per document")
		ref = todo()
		with patch(f"{EX}.frappe.logger") as mock_logger:
			res = executor.run_automation(name, "t1", "ToDo", ref)
			self.assertIsNone(res)
			self.assertEqual(frappe.db.count(RUN_DOCTYPE, {"automation": name}), 0)
			mock_logger.assert_called_with(executor.LOGGER_NAME)
			mock_logger.return_value.warning.assert_called_once()
			log_msg = mock_logger.return_value.warning.call_args[0][0]
			self.assertIn("claim unavailable", log_msg)
			self.assertIn("run skipped", log_msg)

	def test_run_claim_released_when_run_insert_fails(self, *_):
		name = make([sms("s")], [("t1", "", "s")], send_frequency="Once per document")
		ref = todo()
		key = f"ma_run_claim:{name}:ToDo:{ref}:doc"
		real_insert = Document.insert

		def failing_insert(doc, *a, **kw):
			if doc.doctype == RUN_DOCTYPE:
				raise RuntimeError("insert failed")
			return real_insert(doc, *a, **kw)

		try:
			with patch.object(Document, "insert", failing_insert), patch(f"{EX}.frappe.log_error"):
				self.assertIsNone(executor.run_automation(name, "t1", "ToDo", ref))
			self.assertTrue(channels.acquire_lock(key), "the claim must be free after a failed insert")
			channels.release_lock(key)
			self.assertIsNotNone(executor.run_automation(name, "t1", "ToDo", ref))
		finally:
			channels.release_lock(key)

	@patch(f"{CH}.release_lock")
	@patch(f"{CH}.acquire_lock", return_value=False)
	def test_run_claim_held_by_another_job_is_not_released(self, mock_acquire, mock_release, *_):
		name = make([sms("s")], [("t1", "", "s")], send_frequency="Once per document")
		self.assertIsNone(executor.run_automation(name, "t1", "ToDo", todo()))
		mock_release.assert_not_called()


class TestSentCount(unittest.TestCase):
	def test_sent_count_counts_whole_log(self):
		log = [{"step_type": "Send SMS", "result": "sent"}, {"step_type": "Wait", "result": "waiting 1 Days"},
			{"step_type": "Send WhatsApp", "result": "sent"}, {"step_type": "Send Email", "result": "skipped: no recipient"}]
		self.assertEqual(executor._sent(log), 2)


class TestFailedRunCap(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": TITLE}, pluck="name"):
			frappe.db.delete(RUN_DOCTYPE, {"automation": name})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "ZZ exec%")})

	def _fail(self, automation, ref, times, days_ago=0):
		for _ in range(times):
			run = frappe.get_doc({"doctype": RUN_DOCTYPE, "automation": automation, "trigger_id": "t1", "status": "Failed",
				"reference_doctype": "ToDo", "reference_name": ref})
			run.flags.ignore_links = True
			run.insert(ignore_permissions=True)
			if days_ago:
				frappe.db.set_value(RUN_DOCTYPE, run.name, "creation", add_to_date(now_datetime(), days=-days_ago), update_modified=False)

	def _ran(self, automation, ref, frequency="Once per document"):
		return executor._names_already_run(automation, frequency, "ToDo", [ref])

	def test_cap_boundary(self):
		name = make([sms("s")], [("t1", "", "s")], send_frequency="Once per document")
		ref = todo()
		self._fail(name, ref, executor.MAX_FAILED_RUNS - 1)
		self.assertEqual(self._ran(name, ref), set())
		self._fail(name, ref, 1)
		self.assertEqual(self._ran(name, ref), {ref})

	def test_cap_is_per_document_and_per_automation(self):
		a = make([sms("s")], [("t1", "", "s")], send_frequency="Once per document")
		b = make([sms("s")], [("t1", "", "s")], send_frequency="Once per document")
		ref, other = todo(), todo()
		self._fail(a, ref, executor.MAX_FAILED_RUNS)
		self.assertEqual(self._ran(a, other), set())
		self.assertEqual(self._ran(b, ref), set())

	def test_once_per_day_ignores_yesterdays_failures(self):
		name = make([sms("s")], [("t1", "", "s")], send_frequency="Once per day")
		ref = todo()
		self._fail(name, ref, executor.MAX_FAILED_RUNS, days_ago=1)
		self.assertEqual(self._ran(name, ref, "Once per day"), set())
		self._fail(name, ref, executor.MAX_FAILED_RUNS)
		self.assertEqual(self._ran(name, ref, "Once per day"), {ref})

	def test_every_time_is_not_capped(self):
		name = make([sms("s")], [("t1", "", "s")], send_frequency="Every time")
		ref = todo()
		self._fail(name, ref, executor.MAX_FAILED_RUNS + 2)
		self.assertEqual(self._ran(name, ref, "Every time"), set())

	@patch(f"{CH}.send_sms", return_value=True)
	def test_run_automation_skips_a_document_at_the_cap(self, *_):
		name = make([sms("s")], [("t1", "", "s")], send_frequency="Once per document")
		ref = todo()
		self._fail(name, ref, executor.MAX_FAILED_RUNS)
		before = frappe.db.count(RUN_DOCTYPE, {"automation": name})
		self.assertIsNone(executor.run_automation(name, "t1", "ToDo", ref))
		self.assertEqual(frappe.db.count(RUN_DOCTYPE, {"automation": name}), before)

	@patch(f"{CH}.send_whatsapp", return_value="CM-AUT-120-VAL")
	def test_aut120_send_whatsapp_records_channel_message_in_run_log(self, *_):
		name = make([{"step_id": "wa", "step_type": "Send WhatsApp", "template": "test_tpl"}],
			[("t1", "", "wa")])
		ref = todo()
		run = executor.run_automation(name, "t1", "ToDo", ref)
		log = json.loads(frappe.db.get_value(RUN_DOCTYPE, run, "log"))
		entry = next(e for e in log if e["step_id"] == "wa")
		self.assertEqual(entry["result"], "sent")
		self.assertEqual(entry.get("message"), "CM-AUT-120-VAL")
