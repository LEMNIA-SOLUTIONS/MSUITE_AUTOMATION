# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for repointing Automation Step legacy links to Broadcast and Recipient List."""

from __future__ import annotations

import frappe
from frappe.tests import IntegrationTestCase

from msuite_automation.patches.repoint_automation_step_links import execute


class TestRepointAutomationStepLinks(IntegrationTestCase):
	"""Test repointing Automation Step target_bulk_message and recipient_list links."""

	def setUp(self):
		super().setUp()
		self._cleanup_records()

	def tearDown(self):
		self._cleanup_records()
		super().tearDown()

	def _cleanup_records(self):
		for title in [
			"Test Repoint Dry Run",
			"Test Repoint Target Bulk",
			"Test Repoint Recipient List",
			"Test Repoint Unresolved",
			"Test Repoint Second Run",
		]:
			for auto_name in frappe.db.get_all("Messaging Automation", filters={"title": title}, pluck="name"):
				frappe.db.delete("Automation Step", {"parent": auto_name})
				frappe.db.delete("Messaging Automation", {"name": auto_name})

		for bc_title in [
			"Test Repoint BC Dry",
			"Test Repoint BC Live",
			"Test Repoint BC Second",
		]:
			for bc_name in frappe.db.get_all("Broadcast", filters={"title": bc_title}, pluck="name"):
				frappe.db.delete("Broadcast Channel", {"parent": bc_name})
				frappe.db.delete("Broadcast", {"name": bc_name})

		for rl_name in [
			"TEST-REP-RL-LIVE (WhatsApp)",
			"TEST-REP-RL-SECOND (WhatsApp)",
		]:
			if frappe.db.exists("Recipient List", rl_name):
				frappe.db.delete("Recipient List", {"name": rl_name})

		frappe.db.commit()

	def _create_automation_step(self, title: str, step_data: dict) -> tuple[str, str]:
		auto = frappe.get_doc({
			"doctype": "Messaging Automation",
			"title": title,
			"disabled": 1,
			"steps": [step_data],
		})
		auto.flags.ignore_validate = True
		auto.flags.ignore_plan_gate = True
		auto.flags.ignore_links = True
		auto.insert(ignore_permissions=True, ignore_links=True)

		step_name = auto.steps[0].name
		return auto.name, step_name

	def _create_broadcast(self, title: str, legacy_doctype: str, legacy_name: str) -> str:
		bc = frappe.get_doc({
			"doctype": "Broadcast",
			"title": title,
			"recipient_source": "Individual",
			"legacy_doctype": legacy_doctype,
			"legacy_name": legacy_name,
			"channels": [{"channel": "WhatsApp"}],
		})
		bc.flags.ignore_permissions = True
		bc.flags.ignore_mandatory = True
		bc.flags.ignore_validate = True
		bc.flags.ignore_links = True
		bc.insert(ignore_permissions=True, ignore_links=True)
		return bc.name

	def _create_recipient_list(self, name: str) -> str:
		rl = frappe.get_doc({
			"doctype": "Recipient List",
			"name": name,
			"list_name": name,
		})
		rl.flags.ignore_permissions = True
		rl.flags.ignore_mandatory = True
		rl.flags.ignore_validate = True
		rl.flags.ignore_links = True
		rl.insert(ignore_permissions=True, ignore_links=True)
		return rl.name

	def test_dry_run_changes_nothing(self):
		bc_name = self._create_broadcast("Test Repoint BC Dry", "Bulk WhatsApp Message", "LEGACY-BWM-DRY")
		auto_name, step_name = self._create_automation_step(
			"Test Repoint Dry Run",
			{
				"step_id": "step_dry_1",
				"step_type": "Send WhatsApp",
				"delivery_mode": "Bulk campaign",
				"target_bulk_message": "LEGACY-BWM-DRY",
			},
		)

		result = execute(dry_run=True)
		self.assertTrue(result["dry_run"])
		self.assertGreaterEqual(result["repointed"], 1)

		# Verify DB unchanged
		val = frappe.db.get_value("Automation Step", step_name, "target_bulk_message")
		self.assertEqual(val, "LEGACY-BWM-DRY")

	def test_repoints_target_bulk_message_to_broadcast(self):
		bc_name = self._create_broadcast("Test Repoint BC Live", "Bulk WhatsApp Message", "LEGACY-BWM-LIVE")
		auto_name, step_name = self._create_automation_step(
			"Test Repoint Target Bulk",
			{
				"step_id": "step_live_1",
				"step_type": "Send WhatsApp",
				"delivery_mode": "Bulk campaign",
				"target_bulk_message": "LEGACY-BWM-LIVE",
			},
		)

		result = execute(dry_run=False)
		self.assertFalse(result["dry_run"])
		self.assertGreaterEqual(result["repointed"], 1)

		# Verify DB updated to Broadcast name
		val = frappe.db.get_value("Automation Step", step_name, "target_bulk_message")
		self.assertEqual(val, bc_name)

	def test_repoints_recipient_list_to_recipient_list(self):
		rl_name = self._create_recipient_list("TEST-REP-RL-LIVE (WhatsApp)")
		auto_name, step_name = self._create_automation_step(
			"Test Repoint Recipient List",
			{
				"step_id": "step_rl_1",
				"step_type": "Add to Recipient List",
				"recipient_list": "TEST-REP-RL-LIVE",
			},
		)

		result = execute(dry_run=False)
		self.assertFalse(result["dry_run"])
		self.assertGreaterEqual(result["repointed"], 1)

		# Verify DB updated to Recipient List name
		val = frappe.db.get_value("Automation Step", step_name, "recipient_list")
		self.assertEqual(val, rl_name)

	def test_unresolved_name_is_reported_not_blanked(self):
		unresolved_bwm = "UNRESOLVED-BWM-999"
		auto_name, step_name = self._create_automation_step(
			"Test Repoint Unresolved",
			{
				"step_id": "step_unres_1",
				"step_type": "Send WhatsApp",
				"delivery_mode": "Bulk campaign",
				"target_bulk_message": unresolved_bwm,
			},
		)

		result = execute(dry_run=False)
		self.assertIn(unresolved_bwm, result["unresolved"])

		# Verify DB field not blanked
		val = frappe.db.get_value("Automation Step", step_name, "target_bulk_message")
		self.assertEqual(val, unresolved_bwm)

	def test_second_run_repoints_zero(self):
		bc_name = self._create_broadcast("Test Repoint BC Second", "Bulk WhatsApp Message", "LEGACY-BWM-SEC")
		auto_name, step_name = self._create_automation_step(
			"Test Repoint Second Run",
			{
				"step_id": "step_sec_1",
				"step_type": "Send WhatsApp",
				"delivery_mode": "Bulk campaign",
				"target_bulk_message": "LEGACY-BWM-SEC",
			},
		)

		res1 = execute(dry_run=False)
		self.assertGreaterEqual(res1["repointed"], 1)
		self.assertEqual(frappe.db.get_value("Automation Step", step_name, "target_bulk_message"), bc_name)

		res2 = execute(dry_run=False)
		self.assertEqual(res2["repointed"], 0)
		self.assertEqual(res2["unresolved"], [])
