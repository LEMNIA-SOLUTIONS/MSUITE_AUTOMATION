import json
import unittest
from unittest.mock import patch

import frappe

from msuite_automation.msuite_automation.engine import preview

CH = "msuite_automation.msuite_automation.engine.channels"


def automation(**kw):
	base = {
		"doctype": "Messaging Automation", "title": "ZZ preview", "phone_field": "mobile_no", "email_field": "email_id", "send_frequency": "Every time",
		"triggers": [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
		"filters": [{"field": "status", "operator": "=", "value": "Open"}],
		"steps": [
			{"step_id": "s1", "step_type": "Send SMS", "sms_message": "Hi {{ doc.description }}", "idx": 1},
			{"step_id": "i1", "step_type": "If/Else", "condition_logic": "All", "idx": 2,
				"condition_rules": json.dumps([{"field": "description", "operator": "=", "value": "Order 42"}])},
			{"step_id": "y1", "step_type": "Send Email", "email_subject": "S {{ doc.name }}", "email_body": "B", "idx": 3},
			{"step_id": "n1", "step_type": "Send SMS", "sms_message": "no", "idx": 4},
			{"step_id": "w1", "step_type": "Wait", "wait_value": 2, "wait_unit": "Days", "idx": 5},
			{"step_id": "u1", "step_type": "Update Field", "update_field": "status", "update_value": "Closed", "idx": 6},
		],
		"edges": [
			{"source_id": "t1", "source_handle": "", "target_id": "s1"},
			{"source_id": "s1", "source_handle": "", "target_id": "i1"},
			{"source_id": "i1", "source_handle": "yes", "target_id": "y1"},
			{"source_id": "i1", "source_handle": "no", "target_id": "n1"},
			{"source_id": "y1", "source_handle": "", "target_id": "w1"},
			{"source_id": "n1", "source_handle": "", "target_id": "w1"},
			{"source_id": "w1", "source_handle": "", "target_id": "u1"},
		],
	}
	if "steps" in kw and "edges" not in kw:
		steps = kw["steps"]
		base["edges"] = [{"source_id": "t1", "source_handle": "", "target_id": steps[0]["step_id"]}] if steps else []
	base.update(kw)
	return frappe.get_doc(base)


def todo(**kw):
	base = {"doctype": "ToDo", "name": "TD-1", "status": "Open", "description": "Order 42", "mobile_no": "9876543210",
		"email_id": "a@example.com"}
	base.update(kw)
	return frappe._dict(base)


class TestDryRun(unittest.TestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_trigger_check_field_change(self):
		auto_fc = automation(triggers=[{
			"trigger_id": "t1", "trigger_type": "Field Change", "reference_doctype": "ToDo",
			"field": "status", "to_value": "Closed", "from_value": "Open",
		}])
		out_mismatch = preview.dry_run(auto_fc, todo(status="Open"))
		self.assertIn("trigger", out_mismatch)
		self.assertFalse(out_mismatch["trigger"]["matches"])
		self.assertIn("currently", out_mismatch["trigger"]["reason"].lower())

		out_match = preview.dry_run(auto_fc, todo(status="Closed"))
		self.assertTrue(out_match["trigger"]["matches"])
		self.assertIn("currently", out_match["trigger"]["reason"].lower())

	def test_trigger_check_date_based(self):
		from frappe.utils import add_to_date, nowdate
		today = nowdate()
		auto_date = automation(triggers=[{
			"trigger_id": "t1", "trigger_type": "Date Based", "reference_doctype": "ToDo",
			"date_field": "date", "offset_days": 2, "offset_direction": "Before",
		}])
		# 2 days Before target date means record date is today + 2 days
		matching_date = str(add_to_date(today, days=2))
		out_match = preview.dry_run(auto_date, todo(date=matching_date))
		self.assertTrue(out_match["trigger"]["matches"])

		out_mismatch = preview.dry_run(auto_date, todo(date=today))
		self.assertFalse(out_mismatch["trigger"]["matches"])

	def test_trigger_check_doc_event_and_schedule(self):
		auto_event = automation(triggers=[{
			"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save",
		}])
		out_event = preview.dry_run(auto_event, todo())
		self.assertIsNone(out_event["trigger"]["matches"])
		self.assertEqual(out_event["trigger"]["reason"], "not evaluated")

		auto_sched = automation(triggers=[{
			"trigger_id": "t1", "trigger_type": "Schedule", "reference_doctype": "ToDo", "schedule_frequency": "Daily",
		}])
		out_sched = preview.dry_run(auto_sched, todo())
		self.assertIsNone(out_sched["trigger"]["matches"])
		self.assertEqual(out_sched["trigger"]["reason"], "not evaluated")

	@patch(f"{CH}.send_to_phone")
	@patch(f"{CH}.frappe.sendmail")
	@patch("frappe.db.set_value")
	def test_walks_tree_and_sends_nothing(self, set_value, sendmail, send_to_phone):
		doc = todo()
		out = preview.dry_run(automation(), doc)
		sendmail.assert_not_called()
		send_to_phone.assert_not_called()
		set_value.assert_not_called()
		self.assertTrue(out["filters"]["passed"])
		self.assertEqual(out["filters"]["rules"][0]["actual"], "Open")
		ids = [s["step_id"] for s in out["steps"]]
		self.assertEqual(ids, ["s1", "i1", "y1", "w1", "u1"])  # No branch (n1) not taken
		by = {s["step_id"]: s for s in out["steps"]}
		self.assertEqual(by["s1"]["message"], "Hi Order 42")
		self.assertEqual(by["i1"]["branch_taken"], "Yes")
		self.assertEqual(by["y1"]["subject"], "S TD-1")
		self.assertEqual(by["w1"]["wait"], "2 Days")
		self.assertEqual((by["u1"]["field"], by["u1"]["from"], by["u1"]["to"]), ("status", "Open", "Closed"))
		self.assertEqual(doc.status, "Closed")

	def test_filters_fail_and_missing_recipient(self):
		out = preview.dry_run(automation(), todo(status="Closed", mobile_no=None))
		self.assertFalse(out["filters"]["passed"])
		self.assertFalse(out["filters"]["rules"][0]["ok"])
		self.assertEqual(out["steps"][0]["skipped"], "no recipient")

	def test_step_error_is_reported(self):
		a = automation(steps=[{"step_id": "s1", "step_type": "Send SMS", "sms_message": "{{ doc.description | nope }}", "idx": 1}])
		out = preview.dry_run(a, todo())
		self.assertIn("error", out["steps"][0])

	def test_whatsapp_document_print_leaves_no_share_key(self):
		real = frappe.get_doc({"doctype": "ToDo", "description": "9876543210"}).insert(ignore_permissions=True)
		keys = lambda: frappe.db.count("Document Share Key", {"reference_doctype": "ToDo", "reference_docname": real.name})
		before = keys()
		tpl = frappe._dict(name="t", template="Hi {{name}}", parameter_format="NAMED", language_code="en",
			header_type="DOCUMENT", buttons=[])
		a = automation(phone_field="description", filters=[], variable_mappings=[
			{"step_id": "w1", "variable_key": "name", "source_field": "description", "static_fallback": ""}],
			steps=[{"step_id": "w1", "step_type": "Send WhatsApp", "template": "t", "header_source": "Document Print", "idx": 1}])
		with patch(f"{CH}._template", return_value=tpl), patch(f"{CH}.send_to_phone") as send:
			out = preview.dry_run(a, real)
		send.assert_not_called()
		row = out["steps"][0]
		self.assertNotIn("error", row, row)
		self.assertNotIn("{{name}}", row["text"])  # NAMED placeholder filled
		self.assertIn("9876543210", row["text"])
		self.assertEqual(keys(), before)  # share key created while building the header was rolled back

	def test_dry_run_rolls_back_writes(self):
		def writes(*a, **k):
			frappe.get_doc({"doctype": "ToDo", "description": "ZZ preview write"}).insert(ignore_permissions=True)
			return {"to": "91", "message": "x"}
		with patch(f"{CH}.render_sms", side_effect=writes):
			preview.dry_run(automation(steps=[{"step_id": "s1", "step_type": "Send SMS", "sms_message": "x", "idx": 1}]), todo())
		self.assertFalse(frappe.db.exists("ToDo", {"description": "ZZ preview write"}))

	def test_if_else_invalid_condition_rules_error(self):
		a = automation(steps=[
			{"step_id": "i1", "step_type": "If/Else", "condition_logic": "All", "condition_rules": "not json", "idx": 1},
			{"step_id": "n1", "step_type": "Send SMS", "sms_message": "no", "idx": 2},
		], edges=[
			{"source_id": "t1", "target_id": "i1"},
			{"source_id": "i1", "source_handle": "no", "target_id": "n1"},
		])
		out = preview.dry_run(a, todo())
		row = out["steps"][0]
		self.assertIn("error", row)
		self.assertEqual(row["branch_taken"], "No")
		self.assertEqual(len(out["steps"]), 2)

	def test_fan_out_and_join_in_dry_run(self):
		a = automation(steps=[
			{"step_id": "a", "step_type": "Send SMS", "sms_message": "A", "pos_x": 0},
			{"step_id": "b", "step_type": "Send SMS", "sms_message": "B", "pos_x": 100},
			{"step_id": "j", "step_type": "Wait", "wait_value": 1, "wait_unit": "Days"}],
			edges=[{"source_id": "t1", "target_id": "a"}, {"source_id": "t1", "target_id": "b"},
				{"source_id": "a", "target_id": "j"}, {"source_id": "b", "target_id": "j"}])
		out = preview.dry_run(a, todo(), "t1")
		self.assertEqual([s["step_id"] for s in out["steps"]], ["a", "b", "j"])
		self.assertTrue(out["steps"][-1]["joined"])
		self.assertEqual(sorted(out["paths"]), ["p1", "p2"])

	def test_dry_run_update_field_in_memory_and_db_unchanged(self):
		real = frappe.get_doc({
			"doctype": "ToDo",
			"description": "F1 test",
			"status": "Open",
			"priority": "Low",
		}).insert(ignore_permissions=True)
		a = automation(
			filters=[],
			steps=[
				{"step_id": "u1", "step_type": "Update Field", "update_field": "priority", "update_value": "High", "idx": 1},
				{"step_id": "i1", "step_type": "If/Else", "condition_logic": "All", "idx": 2,
					"condition_rules": json.dumps([{"field": "priority", "operator": "=", "value": "High"}])},
			],
			edges=[
				{"source_id": "t1", "source_handle": "", "target_id": "u1"},
				{"source_id": "u1", "source_handle": "", "target_id": "i1"},
			],
		)
		out = preview.dry_run(a, real)
		by = {s["step_id"]: s for s in out["steps"]}
		self.assertEqual(by["u1"]["to"], "High")
		self.assertEqual(by["i1"]["branch_taken"], "Yes")
		self.assertEqual(frappe.db.get_value("ToDo", real.name, "priority"), "Low")

	def test_dry_run_loop_reports_error(self):
		a = automation(
			filters=[],
			steps=[
				{"step_id": "a", "step_type": "Send SMS", "sms_message": "A", "idx": 1},
				{"step_id": "b", "step_type": "Send SMS", "sms_message": "B", "idx": 2},
			],
			edges=[
				{"source_id": "t1", "source_handle": "", "target_id": "a"},
				{"source_id": "a", "source_handle": "", "target_id": "b"},
				{"source_id": "b", "source_handle": "", "target_id": "a"},
			],
		)
		out = preview.dry_run(a, todo())
		loop_steps = [s for s in out["steps"] if "Loop" in s.get("error", "")]
		self.assertTrue(bool(loop_steps))
		self.assertIn("Loop", loop_steps[0]["error"])

	def test_wait_for_event_preview(self):
		a = automation(
			filters=[],
			steps=[
				{"step_id": "we", "step_type": "Wait for Event", "wait_value": 3, "wait_unit": "Days",
					"check_doctype": "Feedback", "check_link_field": "sales_order", "idx": 1},
				{"step_id": "ev", "step_type": "Send SMS", "sms_message": "on event", "idx": 2},
				{"step_id": "to", "step_type": "Send SMS", "sms_message": "on timeout", "idx": 3},
			],
			edges=[
				{"source_id": "t1", "source_handle": "", "target_id": "we"},
				{"source_id": "we", "source_handle": "event", "target_id": "ev"},
				{"source_id": "we", "source_handle": "timeout", "target_id": "to"},
			],
		)
		out = preview.dry_run(a, todo())
		by = {s["step_id"]: s for s in out["steps"]}
		self.assertIn("we", by)
		self.assertIn("ev", by)
		self.assertNotIn("to", by)
		desc = by["we"].get("description", "")
		self.assertIn("Feedback", desc)
		self.assertIn("3 Days", desc)

