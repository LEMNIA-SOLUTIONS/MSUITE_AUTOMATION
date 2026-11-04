import unittest

import frappe

from msuite_core.patches import convert_automations_to_graph as convert

TITLE = "ZZ convert"


def legacy(**kw):
	"""A tree-shaped automation as the Phase 1-3 builder saved it (no edges)."""
	doc = frappe.get_doc({"doctype": "Messaging Automation", "title": TITLE, "disabled": 1,
		"trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save", "phone_field": "description",
		"filters": [{"field": "status", "operator": "=", "value": "Open"}],
		"steps": [{"step_id": "i", "step_type": "If/Else", "condition_rules": "[]", "idx": 1},
			{"step_id": "y", "step_type": "Send SMS", "sms_message": "yes", "parent_step_id": "i", "branch": "Yes", "idx": 2},
			{"step_id": "z", "step_type": "Send SMS", "sms_message": "tail", "idx": 3}], **kw})
	doc.flags.ignore_validate = True
	doc.flags.ignore_plan_gate = True
	doc.insert(ignore_permissions=True, ignore_links=True)
	return doc.name


class TestConvertPatch(unittest.TestCase):
	def tearDown(self):
		frappe.db.rollback()

	def test_apply_converts_tree_to_graph(self):
		name = legacy()
		self.assertIn(name, convert.pending_names())
		convert.apply(name, convert.convert(name))
		doc = frappe.get_doc("Messaging Automation", name)
		t = doc.triggers[0]
		self.assertEqual((t.trigger_type, t.reference_doctype, t.doc_event), ("Document Event", "ToDo", "After Save"))
		self.assertEqual(doc.filters[0].trigger_id, t.trigger_id)
		self.assertEqual(sorted((e.source_id, e.source_handle, e.target_id) for e in doc.edges),
			sorted([(t.trigger_id, "", "i"), ("i", "yes", "y"), ("i", "no", "z"), ("y", "", "z")]))
		self.assertEqual(doc.recipients[0].phone_field, "description")
		self.assertNotIn(name, convert.pending_names())  # idempotent: converted rows are skipped

	def test_problems_disable_the_automation(self):
		name = legacy(disabled=0, trigger_type="Field Change", triggers=[
			{"trigger_id": "t1", "field": "status", "to_value": "A"}, {"trigger_id": "t2", "field": "status", "to_value": "B"}],
			steps=[{"step_id": "s", "step_type": "Send SMS", "sms_message": "x", "idx": 1},
				{"step_id": "x", "step_type": "Send SMS", "sms_message": "x", "trigger_id": "t1", "idx": 2},
				{"step_id": "y", "step_type": "Send SMS", "sms_message": "y", "trigger_id": "t2", "idx": 3}])
		conv = convert.convert(name)
		self.assertTrue(conv["problems"])
		convert.apply(name, conv)
		self.assertEqual(frappe.db.get_value("Messaging Automation", name, "disabled"), 1)

	def test_failure_leaves_row_untouched(self):
		name = legacy()
		before = frappe.db.get_value("Messaging Automation", name, "disabled")
		frappe.db.savepoint("t")
		with self.assertRaises(Exception):
			convert.apply(name, {"triggers": [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
				"filters": [], "edges": [{"source_id": "t1", "source_handle": "", "target_id": "missing"}], "mappings": [], "recipients": [], "problems": []})
		frappe.db.rollback(save_point="t")
		self.assertEqual(frappe.db.get_value("Messaging Automation", name, "disabled"), before)
		self.assertEqual(frappe.db.count("Automation Edge", {"parent": name}), 0)

	def test_idempotent_for_automation_without_steps(self):
		name = legacy(steps=[])
		self.assertIn(name, convert.pending_names())
		convert.apply(name, convert.convert(name))
		self.assertNotIn(name, convert.pending_names())

	def test_legacy_trigger_rows_with_default_trigger_type_are_pending(self):
		name = legacy(
			trigger_type="Field Change",
			reference_doctype="ToDo",
			triggers=[{"trigger_id": "t1", "field": "status", "to_value": "Closed", "trigger_type": "Field Change", "reference_doctype": ""}],
			steps=[{"step_id": "s1", "step_type": "Send SMS", "sms_message": "hi", "idx": 1}],
		)
		self.assertIn(name, convert.pending_names())
