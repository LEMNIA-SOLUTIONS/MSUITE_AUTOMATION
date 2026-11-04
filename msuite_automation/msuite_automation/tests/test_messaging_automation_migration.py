"""Pure-function tests for the Messaging Automation merge patch.

The end-to-end rename/copy is verified against a real site by
`verify_local_migration.run` (see plan Task 2, Step 7) — the old DocTypes no
longer exist after migration, so they can't be fixtures here.
"""

import unittest

from msuite_core.patches.merge_into_messaging_automation import (
	find_clashes,
	lead_row_to_ma,
	never_fired,
	order_row_to_ma,
)


class TestMergeHelpers(unittest.TestCase):
	def test_find_clashes(self):
		self.assertEqual(find_clashes({"WLA-2026-00001", "Promo"}, ["WLA-2026-00001", "OAW-2026-00002"]), ["WLA-2026-00001"])
		self.assertEqual(find_clashes(set(), ["WLA-2026-00001"]), [])

	def test_lead_row_maps_fields(self):
		row = {"name": "WLA-2026-00007", "rule_name": "Won", "enabled": 1, "source_doctype": "Opportunity",
			"to_status": "Won", "from_status": "", "recipient_list": "L1", "template": "T1", "canvas_data": "{}"}
		out = lead_row_to_ma(row)
		self.assertEqual(out["name"], "WLA-2026-00007")
		self.assertEqual(out["notification_type"], "Lead Status Event")
		self.assertEqual(out["title"], "Won")
		self.assertEqual(out["disabled"], 0)
		self.assertEqual(out["reference_doctype"], "Opportunity")
		self.assertEqual(out["to_status"], "Won")
		self.assertNotIn("rule_name", out)
		self.assertNotIn("enabled", out)
		self.assertNotIn("source_doctype", out)

	def test_lead_row_defaults_crm_lead(self):
		out = lead_row_to_ma({"name": "WLA-1", "rule_name": "x", "enabled": 1, "source_doctype": None})
		self.assertEqual(out["reference_doctype"], "CRM Lead")
		self.assertEqual(out["disabled"], 0)

	def test_lead_row_disabled_inverts(self):
		self.assertEqual(lead_row_to_ma({"name": "WLA-1", "rule_name": "x", "enabled": 0})["disabled"], 1)

	def test_order_row_maps_fields(self):
		row = {"name": "OAW-2026-00001", "workflow_name": "Shipped", "disabled": 0, "reference_doctype": "Sales Order",
			"include_return_notes": 1, "canvas_data": "{\"nodes\": []}", "triggers": "[]"}
		out = order_row_to_ma(row)
		self.assertEqual(out["notification_type"], "Order Workflow Event")
		self.assertEqual(out["title"], "Shipped")
		self.assertEqual(out["include_return_notes"], 1)
		self.assertEqual(out["triggers"], "[]")
		self.assertNotIn("workflow_name", out)

	def test_never_fired_scheduler_event(self):
		self.assertTrue(never_fired({"notification_type": "Scheduler Event"}))

	def test_never_fired_days_before(self):
		self.assertTrue(never_fired({"notification_type": "DocType Event", "reference_doctype": "Sales Order", "doctype_event": "Days Before"}))

	def test_fired_on_so_dn_after_insert(self):
		for dt in ("Sales Order", "Delivery Note"):
			self.assertFalse(never_fired({"notification_type": "DocType Event", "reference_doctype": dt, "doctype_event": "After Insert"}))

	def test_never_fired_other_doctype_event(self):
		self.assertTrue(never_fired({"notification_type": "DocType Event", "reference_doctype": "User", "doctype_event": "After Save"}))
		self.assertTrue(never_fired({"notification_type": "DocType Event", "reference_doctype": "Sales Order", "doctype_event": "After Save"}))

	def test_never_fired_lead_on_unhooked_doctype(self):
		self.assertTrue(never_fired({"notification_type": "Lead Status Event", "reference_doctype": "Task"}))
		self.assertTrue(never_fired({"notification_type": "Lead Status Event", "reference_doctype": "Sales Order"}))
		self.assertFalse(never_fired({"notification_type": "Lead Status Event", "reference_doctype": "CRM Lead"}))

	def test_order_workflow_always_fired(self):
		self.assertFalse(never_fired({"notification_type": "Order Workflow Event", "reference_doctype": "Sales Order"}))


class TestLegacyGraphBuilder(unittest.TestCase):
	def test_builds_linear_graph(self):
		import json
		from msuite_core.patches.migrate_order_status_notifications_to_workflows import build_legacy_workflow_graph

		canvas, triggers = build_legacy_workflow_graph(
			{"watched_field": "status", "target_value": "Completed", "send_whatsapp": 1, "template": "t1"}, "Sales Order"
		)
		self.assertEqual([n["type"] for n in json.loads(canvas)["nodes"]], ["source", "status", "channel"])
		self.assertEqual(json.loads(triggers)[0]["target_value"], "Completed")

	def test_invalid_status_without_target_returns_none(self):
		from msuite_core.patches.migrate_order_status_notifications_to_workflows import build_legacy_workflow_graph

		self.assertIsNone(build_legacy_workflow_graph({"watched_field": "status", "target_value": ""}, "Sales Order"))


class TestSilenceNeverFired(unittest.TestCase):
	@unittest.mock.patch("frappe.db.get_default", return_value="1")
	@unittest.mock.patch("frappe.db.sql")
	def test_silence_never_fired_skips_if_marker_exists(self, mock_sql, mock_get_default):
		from msuite_core.patches.merge_into_messaging_automation import _silence_never_fired

		_silence_never_fired()
		mock_get_default.assert_called_with("messaging_automation_silenced")
		mock_sql.assert_not_called()

	@unittest.mock.patch("frappe.db.get_default", return_value=None)
	@unittest.mock.patch("frappe.db.set_default")
	@unittest.mock.patch("frappe.db.sql", return_value=[])
	def test_silence_never_fired_sets_marker(self, mock_sql, mock_set_default, mock_get_default):
		from msuite_core.patches.merge_into_messaging_automation import _silence_never_fired

		_silence_never_fired()
		mock_get_default.assert_called_with("messaging_automation_silenced")
		mock_sql.assert_called_once()
		mock_set_default.assert_called_once_with("messaging_automation_silenced", "1")


