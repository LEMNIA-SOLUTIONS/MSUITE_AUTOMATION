"""AUT-101: an unmapped WhatsApp template variable blocks saving a draft, not just activation."""
import unittest
from unittest.mock import patch

import frappe

from msuite_automation.msuite_automation.doctype.messaging_automation import messaging_automation as ma

TEMPLATE_NAME = "tpl_under_test"
TEMPLATE_TEXT = "Hi {{1}}"
NOT_VALID_TITLE = "Automation is not valid"


def _doc(*, disabled=1, template=TEMPLATE_NAME, mappings=(), delivery_mode=None):
	step = {"step_id": "w", "step_type": "Send WhatsApp", "template": template}
	if delivery_mode:
		step["delivery_mode"] = delivery_mode
	return frappe.get_doc({
		"doctype": "Messaging Automation", "title": "Draft", "disabled": disabled,
		"triggers": [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
		"steps": [step],
		"edges": [{"source_id": "t1", "source_handle": "", "target_id": "w"}],
		"variable_mappings": list(mappings),
	})


def _validate(doc):
	with patch.object(ma.frappe.db, "get_value", return_value={"template": TEMPLATE_TEXT}), \
			patch.object(ma.MessagingAutomation, "_validate_fields_exist"):
		doc.validate()


class TestDraftMappingBlock(unittest.TestCase):
	def test_draft_with_unmapped_variable_is_rejected(self):
		with self.assertRaises(frappe.ValidationError) as ctx:
			_validate(_doc())
		self.assertIn("map {{1}} for ToDo", str(ctx.exception))
		self.assertEqual(frappe.local.message_log[-1].get("title"), NOT_VALID_TITLE)

	def test_mapped_by_source_field_saves(self):
		_validate(_doc(mappings=[{"step_id": "w", "variable_key": "1", "source_field": "description"}]))

	def test_mapped_by_static_fallback_saves(self):
		_validate(_doc(mappings=[{"step_id": "w", "variable_key": "1", "static_fallback": "x"}]))

	def test_no_template_has_no_mapping_issue(self):
		# validate_graph already rejects a step with no template; the mapping check must add nothing.
		with patch.object(ma.frappe.db, "get_value") as get_value:
			self.assertEqual(_doc(template="")._mapping_issues(), [])
		get_value.assert_not_called()

	def test_bulk_campaign_step_is_skipped(self):
		_validate(_doc(delivery_mode="Bulk campaign"))
