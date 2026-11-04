import json
import unittest

from msuite_automation.msuite_automation.migration.convert import convert_row


def graph(nodes, edges):
	return json.dumps({"nodes": nodes, "edges": edges})


class TestLead(unittest.TestCase):
	def test_direct(self):
		out = convert_row({"name": "L1", "notification_type": "Lead Status Event", "reference_doctype": "CRM Lead",
			"from_status": "New", "to_status": "Qualified", "template": "tpl", "recipient_list": "RL",
			"dispatch_mode": "Direct 1:1 Message", "has_workflow_state": 0, "has_status": 1},
			[{"variable_key": "1", "lead_field": "first_name", "static_fallback": "there"}])
		self.assertEqual(out["values"]["trigger_type"], "Field Change")
		self.assertEqual(out["values"]["send_frequency"], "Once per document")
		self.assertEqual(out["triggers"], [{"trigger_id": "t1", "label": "Status → Qualified", "field": "status", "from_value": "New", "to_value": "Qualified"}])
		self.assertEqual([s["step_type"] for s in out["steps"]], ["Add to Recipient List", "Send WhatsApp"])
		self.assertEqual(out["steps"][1]["delivery_mode"], "Direct")
		self.assertEqual(out["mappings"], [{"step_id": "wa", "variable_key": "1", "source_field": "first_name", "static_fallback": "there"}])
		self.assertEqual(out["problems"], [])

	def test_bulk_and_workflow_state(self):
		out = convert_row({"name": "L2", "notification_type": "Lead Status Event", "reference_doctype": "Issue",
			"to_status": "Closed", "template": "tpl", "dispatch_mode": "Bulk Broadcast Campaign", "bulk_schedule_type": "Delay Buffer",
			"bulk_delay_minutes": 5, "use_batched_enqueue": 1, "has_status": 0, "has_workflow_state": 1}, [])
		self.assertEqual(out["triggers"][0]["field"], "workflow_state")
		wa = out["steps"][-1]
		self.assertEqual((wa["delivery_mode"], wa["bulk_schedule_type"], wa["bulk_delay_minutes"]), ("Bulk campaign", "Delay Buffer", 5))


class TestNotification(unittest.TestCase):
	def test_doctype_event_all_channels_and_property(self):
		out = convert_row({"name": "N1", "notification_type": "DocType Event", "reference_doctype": "ToDo", "doctype_event": "After Save",
			"send_whatsapp": 1, "template": "tpl", "field_name": "phone", "attach_document_print": 1, "button_fields": "a,b",
			"send_email": 1, "email_field_name": "owner", "email_subject": "S", "email_body": "B",
			"send_sms": 1, "sms_message": "M", "set_property_after_alert": "status", "property_value": "Closed",
			"condition": "doc.x", "template_variables": json.dumps(["description"])}, [])
		v = out["values"]
		self.assertEqual((v["trigger_type"], v["doc_event"], v["phone_field"], v["email_field"], v["send_frequency"]),
			("Document Event", "After Save", "phone", "owner", "Once per day"))
		self.assertEqual([s["step_type"] for s in out["steps"]], ["Send WhatsApp", "Send Email", "Send SMS", "Update Field"])
		self.assertEqual(out["steps"][0]["header_source"], "Document Print")
		self.assertEqual(out["mappings"], [{"step_id": "wa", "variable_key": "1", "source_field": "description", "static_fallback": ""}])

	def test_days_before(self):
		out = convert_row({"name": "N2", "notification_type": "DocType Event", "reference_doctype": "ToDo", "doctype_event": "Days Before",
			"days_in_advance": 3, "date_changed": "date", "send_whatsapp": 1, "template": "tpl"}, [])
		v = out["values"]
		self.assertEqual((v["trigger_type"], v["date_field"], v["offset_days"], v["offset_direction"]), ("Date Based", "date", 3, "Before"))

	def test_scheduler(self):
		out = convert_row({"name": "N3", "notification_type": "Scheduler Event", "event_frequency": "Hourly", "send_whatsapp": 1, "template": "tpl",
			"condition": "doc._contact_list=['1']"}, [])
		self.assertEqual((out["values"]["trigger_type"], out["values"]["schedule_frequency"]), ("Schedule", "Hourly"))

	def test_no_channel_is_a_problem(self):
		out = convert_row({"name": "N4", "notification_type": "DocType Event", "reference_doctype": "ToDo", "doctype_event": "After Save"}, [])
		self.assertTrue(out["problems"])


class TestOrder(unittest.TestCase):
	def test_branching_multi_status(self):
		canvas = graph(
			[{"id": "s1", "type": "status", "data": {"watched_field": "status", "target_value": "Shipped"}},
			 {"id": "s2", "type": "status", "data": {"watched_field": "lr_no", "target_value": ""}},
			 {"id": "c1", "type": "condition", "data": {"logic": "OR", "rules": [{"field": "grand_total", "operator": ">", "value": "500"}]}},
			 {"id": "ch1", "type": "channel", "data": {"send_whatsapp": 1, "template": "t1", "send_sms": 1, "sms_message": "hi",
				"variable_mappings": [{"variable_key": "1", "source_field": "customer_name"}]}},
			 {"id": "p1", "type": "property", "data": {"set_property_after_alert": "notified", "property_value": "1"}},
			 {"id": "ch2", "type": "channel", "data": {"send_email": 1, "email_subject": "x", "email_field_name": "contact_email"}}],
			[{"from": "s1", "to": "c1"}, {"from": "c1", "to": "ch1", "branch": "true"}, {"from": "c1", "to": "p1", "branch": "false"},
			 {"from": "ch1", "to": "p1"}, {"from": "s2", "to": "ch2"}])
		out = convert_row({"name": "O1", "notification_type": "Order Workflow Event", "reference_doctype": "Sales Order",
			"canvas_data": canvas, "include_return_notes": 0}, [])
		self.assertEqual(out["problems"], [])
		self.assertEqual(out["values"]["skip_return_documents"], 1)
		self.assertEqual(out["values"]["send_frequency"], "Every time")
		self.assertEqual([(t["trigger_id"], t["field"], t["to_value"]) for t in out["triggers"]], [("s1", "status", "Shipped"), ("s2", "lr_no", "")])
		ids = {s["step_id"]: s for s in out["steps"]}
		self.assertEqual(ids["c1"]["step_type"], "If/Else")
		self.assertEqual(json.loads(ids["c1"]["condition_rules"])[0]["field"], "grand_total")
		self.assertEqual(ids["c1"]["condition_logic"], "Any")
		self.assertEqual((ids["ch1"]["parent_step_id"], ids["ch1"]["branch"], ids["ch1"]["step_type"]), ("c1", "Yes", "Send WhatsApp"))
		self.assertEqual((ids["ch1-sms"]["parent_step_id"], ids["ch1-sms"]["branch"]), ("c1", "Yes"))
		# p1 reached twice (from c1/No and after ch1) → duplicated
		# Ruling P2: edge order visits Yes branch (ch1 -> p1) first, so p1 has Yes and p1-2 has No
		self.assertEqual((ids["p1"]["branch"], ids["p1"]["step_type"]), ("Yes", "Update Field"))
		self.assertEqual((ids["p1-2"]["branch"], ids["p1-2"]["parent_step_id"]), ("No", "c1"))
		self.assertEqual(ids["ch2"]["trigger_id"], "s2")
		self.assertEqual(ids["ch2"]["step_type"], "Send Email")
		self.assertEqual(out["mappings"], [{"step_id": "ch1", "variable_key": "1", "source_field": "customer_name", "static_fallback": ""}])

	def test_assigned_changed_and_cycle(self):
		canvas = graph(
			[{"id": "s1", "type": "status", "data": {"watched_field": "status", "target_value": "Assigned / Changed"}},
			 {"id": "a", "type": "condition", "data": {"rules": []}}, {"id": "b", "type": "condition", "data": {"rules": []}}],
			[{"from": "s1", "to": "a"}, {"from": "a", "to": "b", "branch": "true"}, {"from": "b", "to": "a", "branch": "true"}])
		out = convert_row({"name": "O2", "notification_type": "Order Workflow Event", "reference_doctype": "Delivery Note", "canvas_data": canvas}, [])
		self.assertEqual(out["triggers"][0]["to_value"], "")
		self.assertTrue(any("cycle" in p for p in out["problems"]))

	def test_blank_target_non_awb_is_problem(self):
		canvas = graph([{"id": "s1", "type": "status", "data": {"watched_field": "status", "target_value": ""}}], [])
		out = convert_row({"name": "O3", "notification_type": "Order Workflow Event", "reference_doctype": "Sales Order", "canvas_data": canvas}, [])
		self.assertTrue(out["problems"])

	def test_before_delete_mapped_to_on_trash(self):
		out = convert_row({"name": "N5", "notification_type": "DocType Event", "reference_doctype": "ToDo",
			"doctype_event": "Before Delete", "send_sms": 1, "sms_message": "bye"}, [])
		self.assertEqual(out["values"]["doc_event"], "On Trash")

	def test_notification_wa_without_template_problem(self):
		out = convert_row({"name": "N6", "notification_type": "DocType Event", "reference_doctype": "ToDo",
			"doctype_event": "After Save", "send_whatsapp": 1, "template": ""}, [])
		self.assertIn("WhatsApp template is required", out["problems"])

	def test_order_wa_without_template_problem(self):
		canvas = graph([
			{"id": "s1", "type": "status", "data": {"watched_field": "status", "target_value": "Shipped"}},
			{"id": "ch1", "type": "channel", "data": {"send_whatsapp": 1, "template": ""}}
		], [{"from": "s1", "to": "ch1"}])
		out = convert_row({"name": "O4", "notification_type": "Order Workflow Event", "reference_doctype": "Sales Order",
			"canvas_data": canvas}, [])
		self.assertIn("WhatsApp template is required", out["problems"])

	def test_scheduler_without_condition_problem(self):
		out = convert_row({"name": "N7", "notification_type": "Scheduler Event", "event_frequency": "Hourly",
			"send_whatsapp": 1, "template": "tpl", "condition": ""}, [])
		self.assertIn("Scheduler rule without condition script — legacy never sent", out["problems"])

	def test_lead_mappings_empty_variable_key_preserves_indices(self):
		out = convert_row({"name": "L3", "notification_type": "Lead Status Event", "reference_doctype": "CRM Lead",
			"to_status": "Won", "template": "tpl", "has_status": 1},
			[{"lead_field": "first_name"}, {"lead_field": "last_name"}])
		self.assertEqual([m["variable_key"] for m in out["mappings"]], ["1", "2"])
