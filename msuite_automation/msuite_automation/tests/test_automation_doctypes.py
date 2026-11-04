import unittest

import frappe

from msuite_automation.msuite_automation import constants as C


class TestAutomationDoctypes(unittest.TestCase):
	def test_feature_for(self):
		self.assertEqual(C.feature_for(C.FIELD_CHANGE, "CRM Lead"), "lead_automation")
		self.assertEqual(C.feature_for(C.FIELD_CHANGE, "Sales Order"), "order_automation")
		self.assertEqual(C.feature_for(C.FIELD_CHANGE, "Delivery Note"), "order_automation")
		self.assertEqual(C.feature_for(C.FIELD_CHANGE, "ToDo"), "notifications")
		self.assertEqual(C.feature_for(C.DOCUMENT_EVENT, "CRM Lead"), "notifications")
		self.assertEqual(C.feature_for(C.SCHEDULE, None), "notifications")

	def test_child_doctypes_exist(self):
		for dt, fields in {
			"Automation Trigger": ("trigger_id", "label", "field", "from_value", "to_value"),
			"Automation Filter": ("field", "operator", "value"),
			"Automation Step": ("step_id", "trigger_id", "parent_step_id", "branch", "step_type", "template",
				"delivery_mode", "wait_value", "wait_unit", "condition_logic", "condition_rules",
				"update_field", "update_value", "recipient_list"),
			"Automation Variable Mapping": ("step_id", "variable_key", "source_field", "static_fallback"),
			"Messaging Automation Run": ("automation", "trigger_id", "reference_doctype", "reference_name", "status",
				"resume_step_id", "resume_at", "context", "log"),
		}.items():
			meta = frappe.get_meta(dt)
			for f in fields:
				self.assertTrue(meta.has_field(f), f"{dt}.{f}")
		self.assertTrue(frappe.get_meta("Automation Step").istable)
		self.assertFalse(frappe.get_meta("Messaging Automation Run").istable)

	def test_graph_schema(self):
		import frappe

		has = lambda dt, f: frappe.get_meta(dt).has_field(f)
		for f in ("trigger_type", "reference_doctype", "doc_event", "date_field", "offset_days", "offset_direction",
				"schedule_frequency", "filter_logic", "condition", "pos_x", "pos_y"):
			self.assertTrue(has("Automation Trigger", f), f)
		self.assertFalse(frappe.get_meta("Automation Trigger").get_field("field").reqd)
		self.assertTrue(has("Automation Filter", "trigger_id"))
		for f in ("switch_field", "switch_cases", "pos_x", "pos_y", "send_to", "whatsapp_account", "sms_account"):
			self.assertTrue(has("Automation Step", f), f)
		self.assertIn("Switch", frappe.get_meta("Automation Step").get_field("step_type").options.split("\n"))
		self.assertTrue(has("Automation Variable Mapping", "reference_doctype"))
		for f in ("source_id", "source_handle", "target_id"):
			self.assertTrue(has("Automation Edge", f), f)
		for f in ("reference_doctype", "phone_field", "email_field"):
			self.assertTrue(has("Automation Recipient", f), f)
		for f in ("note_id", "text", "color", "pos_x", "pos_y", "width", "height"):
			self.assertTrue(has("Automation Note", f), f)
		for f in ("path_id", "node_id", "status", "executed", "joined", "resume_at"):
			self.assertTrue(has("Automation Run Path", f), f)
		ma = frappe.get_meta("Messaging Automation")
		for t, opt in (("edges", "Automation Edge"), ("recipients", "Automation Recipient"), ("notes", "Automation Note")):
			self.assertEqual(ma.get_field(t).options, opt)
		self.assertFalse(ma.get_field("trigger_type").reqd)
		self.assertEqual(frappe.get_meta("Messaging Automation Run").get_field("paths").options, "Automation Run Path")

	def test_graph_constants(self):
		from msuite_automation.msuite_automation import constants as c

		self.assertEqual(c.SWITCH, "Switch")
		self.assertIn(c.SWITCH, c.STEP_TYPES)
		self.assertEqual(set(c.ACTION_TYPES), {c.SEND_WHATSAPP, c.SEND_EMAIL, c.SEND_SMS, c.UPDATE_FIELD, c.ADD_TO_LIST, c.SEND_IG_PUBLIC_REPLY, c.SEND_IG_CONSENT_TEMPLATE})
		self.assertEqual((c.PATH_RUNNING, c.PATH_WAITING, c.PATH_AT_JOIN, c.PATH_ENDED), ("Running", "Waiting", "At join", "Ended"))
