import json
import unittest
from unittest.mock import patch

import frappe
from frappe.utils import add_to_date, now_datetime

from msuite_automation.msuite_automation.constants import RUN_DOCTYPE, WAIT_EVENT
from msuite_automation.msuite_automation.engine import event_check, executor
from msuite_automation.msuite_automation.engine import triggers as T

CH = "msuite_automation.msuite_automation.engine.channels"
TITLE = "ZZ wait event hook test"


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
	T.clear_cache()
	return doc.name


def todo(desc="ZZ wait event hook test", **kw):
	return frappe.get_doc({"doctype": "ToDo", "description": desc, **kw}).insert(ignore_permissions=True).name


def sms(sid, msg="hi"):
	return {"step_id": sid, "step_type": "Send SMS", "sms_message": msg}


@patch(f"{CH}.send_whatsapp", return_value=True)
@patch(f"{CH}.send_email", return_value=True)
@patch(f"{CH}.send_sms", return_value=True)
class TestWaitEventHook(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": TITLE}, pluck="name"):
			frappe.db.delete(RUN_DOCTYPE, {"automation": name})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "%98765 43210%")})
		frappe.db.delete("ToDo", {"description": ("like", "ZZ wait event hook%")})
		frappe.db.delete("Channel Message", {"text_content": ("like", "ZZ hook msg%")})
		for note in frappe.get_all("Note", filters={"title": ("like", "ZZ %")}, pluck="name"):
			frappe.delete_doc("Note", note, force=True, ignore_permissions=True)
		T.clear_cache()

	def test_wait_key_same_doc_is_empty(self, *_):
		step = {"step_id": "we1", "step_type": WAIT_EVENT, "condition_rules": "[]"}
		doc = frappe._dict(doctype="ToDo", name="T1", description="test")
		self.assertEqual(event_check.wait_key(step, doc), "")

	def test_wait_key_linked_phone_canonical(self, *_):
		step = {
			"step_id": "we1",
			"step_type": WAIT_EVENT,
			"check_doctype": "Channel Message",
			"check_link_field": "sender_phone",
			"check_match_field": "contact_phone",
		}
		doc1 = frappe._dict(doctype="Sales Order", name="SO1", contact_phone="+91 98765 43210")
		doc2 = frappe._dict(doctype="Sales Order", name="SO2", contact_phone="919876543210")
		key1 = event_check.wait_key(step, doc1)
		key2 = event_check.wait_key(step, doc2)
		self.assertEqual(key1, "919876543210")
		self.assertEqual(key1, key2)

	def test_wait_key_empty_value(self, *_):
		step = {
			"step_id": "we1",
			"step_type": WAIT_EVENT,
			"check_doctype": "Channel Message",
			"check_link_field": "sender_phone",
			"check_match_field": "contact_phone",
		}
		doc_empty = frappe._dict(doctype="Sales Order", name="SO1", contact_phone="")
		doc_none = frappe._dict(doctype="Sales Order", name="SO2", contact_phone=None)
		self.assertEqual(event_check.wait_key(step, doc_empty), "")
		self.assertEqual(event_check.wait_key(step, doc_none), "")
		self.assertEqual(event_check.wait_key(step, None), "")

	def test_wait_key_long_value_returns_empty(self, *_):
		step = {
			"step_id": "we1",
			"step_type": WAIT_EVENT,
			"check_doctype": "Note",
			"check_link_field": "title",
			"check_match_field": "title",
		}
		doc = frappe._dict(doctype="Note", name="N1", title="x" * 200)
		self.assertEqual(event_check.wait_key(step, doc), "")

	def test_wait_key_phone_link_field_digits_only(self, *_):
		step = {
			"step_id": "we1",
			"step_type": WAIT_EVENT,
			"check_doctype": "Channel Message",
			"check_link_field": "sender_phone",
			"check_match_field": "contact_phone",
		}
		doc = frappe._dict(doctype="Sales Order", name="SO1", contact_phone="abc 9876543210")
		self.assertEqual(event_check.wait_key(step, doc), "9876543210")

	def test_watched_record_save_enqueues_one_check_job_and_unwatched_enqueues_nothing(self, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 2, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
		], [("t1", "", "we"), ("we", "event", "on_event")])

		T.clear_cache()
		self.assertIn("ToDo", T.get_watch_map())

		with patch("frappe.enqueue") as mock_enqueue:
			todo_doc = frappe.get_doc({"doctype": "ToDo", "description": "ZZ wait event hook test save", "status": "Open"})
			todo_doc.insert(ignore_permissions=True)
			frappe.db.after_commit.run()
			check_calls = [
				c for c in mock_enqueue.call_args_list
				if "check_event_waits" in str(c)
			]
			self.assertEqual(len(check_calls), 1)
			self.assertEqual(check_calls[0].kwargs.get("doctype"), "ToDo")
			self.assertEqual(check_calls[0].kwargs.get("name"), todo_doc.name)
			self.assertEqual(check_calls[0].kwargs.get("job_id"), f"check_event_waits:ToDo:{todo_doc.name}")
			self.assertTrue(check_calls[0].kwargs.get("deduplicate"))

		# Unwatched doctype save must enqueue nothing
		with patch("frappe.enqueue") as mock_enqueue:
			note_doc = frappe.get_doc({"doctype": "Note", "title": "ZZ unwatched test"})
			note_doc.insert(ignore_permissions=True)
			frappe.db.after_commit.run()
			check_calls = [
				c for c in mock_enqueue.call_args_list
				if "check_event_waits" in str(c)
			]
			self.assertEqual(len(check_calls), 0)

	def test_check_event_waits_truncation_warning(self, *_):
		td = todo(status="Open")
		with patch.object(executor, "EVENT_CHECK_CAP", 1), patch("frappe.logger") as mock_logger:
			with patch("frappe.db.sql_list", return_value=["RUN-001", "RUN-002"]), patch.object(executor, "resume_waiting_runs"):
				executor.check_event_waits("ToDo", td)
			mock_logger.return_value.warning.assert_called_once()
			msg = mock_logger.return_value.warning.call_args[0][0]
			self.assertIn("1 candidates dropped", msg)
			self.assertIn("10-minute poll", msg)

	def test_check_event_waits_resumes_only_matching_run_and_leaves_others_waiting(self, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 2, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
			sms("on_timeout", "timed out"),
		], [("t1", "", "we"), ("we", "event", "on_event"), ("we", "timeout", "on_timeout")])

		todo1 = todo(status="Open")
		todo2 = todo(status="Open")

		run1 = executor.run_automation(name, "t1", "ToDo", todo1)
		run2 = executor.run_automation(name, "t1", "ToDo", todo2)

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run1, "status"), "Waiting")
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run2, "status"), "Waiting")

		# Close todo1 only
		frappe.db.set_value("ToDo", todo1, "status", "Closed")

		# Run check_event_waits for todo1
		executor.check_event_waits("ToDo", todo1)

		# run1 should be Completed, run2 must remain Waiting
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run1, "status"), "Completed")
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run2, "status"), "Waiting")

	def test_two_check_event_waits_for_same_run_resumes_once(self, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 2, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
			sms("on_event", "event met"),
		], [("t1", "", "we"), ("we", "event", "on_event")])

		td = todo(status="Open")
		run = executor.run_automation(name, "t1", "ToDo", td)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")

		frappe.db.set_value("ToDo", td, "status", "Closed")

		# Call check_event_waits twice
		executor.check_event_waits("ToDo", td)
		executor.check_event_waits("ToDo", td)

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		log = json.loads(frappe.db.get_value(RUN_DOCTYPE, run, "log"))
		event_steps = [e for e in log if e.get("step_id") == "on_event" and e.get("result") == "sent"]
		self.assertEqual(len(event_steps), 1)

	def test_record_whose_phone_does_not_match_resumes_nothing(self, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 2, "wait_unit": "Days",
				"check_doctype": "Channel Message", "check_link_field": "sender_phone", "check_match_field": "description",
				"condition_rules": json.dumps([{"field": "direction", "operator": "=", "value": "Inbound"}])},
			sms("on_event", "reply received"),
		], [("t1", "", "we"), ("we", "event", "on_event")],
		triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}])

		td_name = todo(desc="+91 98765 43210", status="Open")
		run = executor.run_automation(name, "t1", "ToDo", td_name)
		self.assertIsNotNone(run)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")

		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["wait_key", "wait_for_event"])
		self.assertEqual(len(paths), 1)
		self.assertEqual(paths[0].wait_key, "919876543210")

		# Create an inbound Channel Message with a DIFFERENT phone
		other_msg = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "WhatsApp",
			"direction": "Inbound",
			"sender_phone": "+91 91111 22222",
			"text_content": "ZZ hook msg irrelevant reply",
		})
		other_msg.flags.suppress_side_effects = True
		other_msg.insert(ignore_permissions=True, ignore_mandatory=True)

		executor.check_event_waits("Channel Message", other_msg.name)

		# Run must remain Waiting
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")

		# Now create an inbound Channel Message with the MATCHING phone
		match_msg = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "WhatsApp",
			"direction": "Inbound",
			"sender_phone": "919876543210",
			"text_content": "ZZ hook msg matching reply",
		})
		match_msg.flags.suppress_side_effects = True
		match_msg.insert(ignore_permissions=True, ignore_mandatory=True)

		executor.check_event_waits("Channel Message", match_msg.name)

		# Run should now be Completed
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")

	def test_disabled_automation_not_in_watch_map(self, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 2, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
		], [("t1", "", "we")], disabled=1)
		T.clear_cache()
		watch_map = T.get_watch_map()
		todo_watches = [w for w in watch_map.get("ToDo", []) if w[0] == name]
		self.assertEqual(len(todo_watches), 0)

	def test_clear_cache_refreshes_watch_map(self, *_):
		name = make([
			{"step_id": "we", "step_type": WAIT_EVENT, "wait_value": 2, "wait_unit": "Days",
				"condition_rules": json.dumps([{"field": "status", "operator": "=", "value": "Closed"}])},
		], [("t1", "", "we")], disabled=0)
		T.clear_cache()
		m1 = T.get_watch_map()
		self.assertTrue(any(w[0] == name for w in m1.get("ToDo", [])))

		frappe.db.set_value("Messaging Automation", name, "disabled", 1)

		# Before clear_cache, still present in cache
		m_cached = T.get_watch_map()
		self.assertTrue(any(w[0] == name for w in m_cached.get("ToDo", [])))

		# After clear_cache, refreshed and excluded
		T.clear_cache()
		m2 = T.get_watch_map()
		self.assertFalse(any(w[0] == name for w in m2.get("ToDo", [])))


class TestChannelMessageSkip(unittest.TestCase):
	def test_channel_message_watched_but_never_fires_trigger_automations(self):
		doc = frappe.get_doc({"doctype": "Channel Message", "name": "ZZ-CM-1"})
		trigger_entry = {"events": {"After Save": [["AUTO-X", "t1"]]}, "fields": []}
		with patch.object(T, "get_watch_map", return_value={"Channel Message": [("AUTO-W", "w", "linked", "phone")]}), \
			patch.object(T, "get_map", return_value={"Channel Message": trigger_entry}), \
			patch("frappe.enqueue") as enq, \
			patch("msuite_automation.msuite_automation.engine.executor.enqueue_run") as run:
			T.on_doc_event(doc, "after_insert")
			frappe.db.after_commit.run()
		self.assertEqual(enq.call_count, 1)  # the Wait-for-Event check job
		run.assert_not_called()  # but no Document Event trigger fires on message saves


@patch(f"{CH}.send_whatsapp", return_value=True)
@patch(f"{CH}.send_email", return_value=True)
@patch(f"{CH}.send_sms", return_value=True)
class TestWaitEventSafetyNet(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": TITLE}, pluck="name"):
			frappe.db.delete(RUN_DOCTYPE, {"automation": name})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "%98765 43210%")})
		frappe.db.delete("ToDo", {"description": ("like", "ZZ wait event hook%")})
		frappe.db.delete("Channel Message", {"text_content": ("like", "ZZ hook msg%")})
		for note in frappe.get_all("Note", filters={"title": ("like", "ZZ %")}, pluck="name"):
			frappe.delete_doc("Note", note, force=True, ignore_permissions=True)
		T.clear_cache()

	def test_missed_hook_resumed_by_poll_event_waits(self, sms_, *_):
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

		# Condition met in DB without running check_event_waits (missed save hook)
		frappe.db.set_value("ToDo", ref, "status", "Closed")

		# poll_event_waits acts as safety net and resumes it
		executor.poll_event_waits()

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		log = json.loads(frappe.db.get_value(RUN_DOCTYPE, run, "log"))
		event_steps = [e for e in log if e.get("step_id") == "on_event"]
		self.assertEqual(len(event_steps), 1)
		self.assertGreaterEqual(sms_.call_count, 1)

	def test_resume_waiting_runs_alone_does_not_resume_not_timed_out_event_run(self, sms_, *_):
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

		# Condition met in DB, but timeout is 2 days in the future
		frappe.db.set_value("ToDo", ref, "status", "Closed")

		# resume_waiting_runs() alone (the every-minute job) must NOT resume it
		executor.resume_waiting_runs()

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Waiting")
		self.assertEqual(sms_.call_count, 0)

	def test_timed_out_event_path_resumes_from_resume_waiting_runs_with_timeout_handle(self, sms_, *_):
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

		# Condition NOT met, but timeout reached in past
		past = add_to_date(now_datetime(), minutes=-10)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.set_value(RUN_DOCTYPE, run, "resume_at", past)

		# resume_waiting_runs() (minute job) resumes timed-out run via resume_at
		executor.resume_waiting_runs()

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run, "status"), "Completed")
		log = json.loads(frappe.db.get_value(RUN_DOCTYPE, run, "log"))
		timeout_steps = [e for e in log if e.get("step_id") == "on_timeout"]
		event_steps = [e for e in log if e.get("step_id") == "on_event"]
		self.assertEqual(len(timeout_steps), 1)
		self.assertEqual(len(event_steps), 0)
		self.assertGreaterEqual(sms_.call_count, 1)

	def test_hooks_scheduler_events_wiring(self, *_):
		from msuite_automation import hooks
		sched = hooks.scheduler_events.get("cron", {})
		ten_min = sched.get("*/10 * * * *", [])
		every_min = sched.get("* * * * *", [])
		target = "msuite_automation.msuite_automation.engine.executor.poll_event_waits"
		self.assertIn(target, ten_min)
		self.assertNotIn(target, every_min)


class TestHookEnqueueFailure(unittest.TestCase):
	def tearDown(self):
		"""Roll back pending transaction changes."""
		frappe.db.rollback()

	def test_redis_error_on_check_job_does_not_abort_commit(self):
		"""Verify Redis error when queuing event wait check does not abort transaction commit."""
		doc = frappe.get_doc({"doctype": "Channel Message", "name": "ZZ-CM-2"})
		with patch.object(T, "get_watch_map", return_value={"Channel Message": [("AUTO-W", "w", "linked", "phone")]}), \
			patch.object(T, "get_map", return_value={}), \
			patch("frappe.enqueue", side_effect=ConnectionError("redis down")), patch("frappe.log_error") as log:
			T.on_doc_event(doc, "after_insert")
			frappe.db.after_commit.run()  # must not raise
		log.assert_called_once()
