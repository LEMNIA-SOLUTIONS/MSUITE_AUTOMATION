# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for lead status automation Broadcast path gated by msuite_broadcast_enabled."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from msuite_automation.msuite_automation.engine import channels

CH = "msuite_automation.msuite_automation.engine.channels"


class TestBroadcastPath(unittest.TestCase):
	"""Test suite for unified Recipient List and Broadcast lead status automation."""

	def setUp(self):
		self.created_broadcasts: list[str] = []
		self.created_recipient_lists: list[str] = []

	def tearDown(self):
		for name in self.created_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True)
		for name in self.created_recipient_lists:
			if frappe.db.exists("Recipient List", name):
				frappe.delete_doc("Recipient List", name, force=True)
		frappe.db.commit()

	def _make_automation(self, name: str = "MA-TEST-RULE-1", title: str = "Test Rule"):
		return frappe._dict(
			name=name,
			title=title,
			whatsapp_account="WA-ACC-001",
			_trigger_label="Status",
		)

	def _make_step(self, template: str = "hello_template", recipient_list: str | None = None):
		return frappe._dict(
			step_id="wa_step_1",
			template=template,
			recipient_list=recipient_list,
			bulk_schedule_type="Immediate",
			use_batched_enqueue=1,
		)

	def _make_lead(self, name: str = "LEAD-001", mobile_no: str = "+91 98765 43210", lead_name: str = "John Doe"):
		return frappe._dict(
			doctype="CRM Lead",
			name=name,
			lead_name=lead_name,
			first_name=lead_name.split()[0],
			last_name=lead_name.split()[-1] if " " in lead_name else "",
			mobile_no=mobile_no,
		)

	def test_flag_off_calls_old_path_not_new(self):
		"""With msuite_broadcast_enabled falsy, legacy Bulk WhatsApp Message functions run."""
		auto = self._make_automation()
		step = self._make_step()
		lead = self._make_lead()

		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": False}),
			patch(f"{CH}._dispatch_bulk_broadcast") as mock_new_dispatch,
			patch(f"{CH}._dispatch_bulk_legacy", return_value={"status": "success", "mode": "legacy"}) as mock_old_dispatch,
		):
			res = channels.dispatch_bulk(auto, step, lead, "+919876543210", {})
			mock_old_dispatch.assert_called_once()
			mock_new_dispatch.assert_not_called()
			self.assertEqual(res.get("mode"), "legacy")

		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": False}),
			patch(f"{CH}._add_to_recipient_list") as mock_new_add,
			patch(f"{CH}._add_to_legacy_list", return_value=True) as mock_old_add,
		):
			added = channels.add_to_list("Legacy-List", "+919876543210", lead)
			mock_old_add.assert_called_once_with("Legacy-List", "+919876543210", lead)
			mock_new_add.assert_not_called()
			self.assertTrue(added)

	def test_flag_on_creates_list_and_broadcast_with_right_channel_row_fields(self):
		"""With flag on, creates Recipient List + Broadcast with correct Broadcast Channel fields."""
		rule_id = f"MA-TEST-{frappe.generate_hash(length=6)}"
		auto = self._make_automation(name=rule_id, title="Onboarding Leads")
		step = self._make_step()
		lead = self._make_lead(mobile_no="+91 98765 43210")

		with patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}):
			res = channels.dispatch_bulk(auto, step, lead, "+91 98765 43210", {"1": "John"})

		self.assertEqual(res.get("status"), "success")
		self.assertEqual(res.get("mode"), "created")
		bc_name = res.get("broadcast")
		self.assertTrue(bc_name)
		self.created_broadcasts.append(bc_name)

		# Verify Broadcast
		bc_doc = frappe.get_doc("Broadcast", bc_name)
		self.assertEqual(bc_doc.recipient_source, "Recipient List")
		self.assertTrue(bc_doc.recipient_list)
		self.created_recipient_lists.append(bc_doc.recipient_list)
		self.assertEqual(bc_doc.total_recipients, 1)

		# Verify Broadcast Channel row
		self.assertEqual(len(bc_doc.channels), 1)
		ch_row = bc_doc.channels[0]
		self.assertEqual(ch_row.channel, "WhatsApp")
		self.assertEqual(ch_row.whatsapp_account, "WA-ACC-001")
		self.assertEqual(ch_row.whatsapp_template, "hello_template")
		self.assertEqual(ch_row.is_lead_automation_broadcast, 1)
		self.assertEqual(ch_row.lead_automation_rule, rule_id)
		self.assertEqual(ch_row.recipient_count, 1)

		# Verify Recipient List contact
		rec_list = frappe.get_doc("Recipient List", bc_doc.recipient_list)
		self.assertEqual(len(rec_list.contacts), 1)
		contact = rec_list.contacts[0]
		self.assertEqual(contact.phone_e164, "+919876543210")
		self.assertEqual(contact.full_name, "John Doe")

	def test_second_lead_appends_to_same_list_and_broadcast(self):
		"""Second incoming lead appends to the same list and Broadcast and updates recipient_count."""
		rule_id = f"MA-TEST-{frappe.generate_hash(length=6)}"
		auto = self._make_automation(name=rule_id, title="Lead Status Step")
		step = self._make_step()
		lead1 = self._make_lead(name="LEAD-1", mobile_no="+91 98765 43210", lead_name="Alice")
		lead2 = self._make_lead(name="LEAD-2", mobile_no="+91 98765 43211", lead_name="Bob")

		with patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}):
			res1 = channels.dispatch_bulk(auto, step, lead1, "+91 98765 43210", {})
			self.assertEqual(res1.get("status"), "success")
			self.assertEqual(res1.get("mode"), "created")
			bc1 = res1.get("broadcast")
			self.created_broadcasts.append(bc1)

			res2 = channels.dispatch_bulk(auto, step, lead2, "+91 98765 43211", {})
			self.assertEqual(res2.get("status"), "success")
			self.assertEqual(res2.get("mode"), "aggregated")
			bc2 = res2.get("broadcast")

		self.assertEqual(bc1, bc2, "Second lead should aggregate onto the same Broadcast document.")

		# Verify counts on Broadcast & channel row
		bc_doc = frappe.get_doc("Broadcast", bc1)
		self.created_recipient_lists.append(bc_doc.recipient_list)
		self.assertEqual(bc_doc.total_recipients, 2)
		self.assertEqual(bc_doc.channels[0].recipient_count, 2)

		# Verify Recipient List has both contacts
		rec_list = frappe.get_doc("Recipient List", bc_doc.recipient_list)
		self.assertEqual(len(rec_list.contacts), 2)
		phones = {c.phone_e164 for c in rec_list.contacts}
		self.assertEqual(phones, {"+919876543210", "+919876543211"})

	def test_duplicate_lead_not_added_twice(self):
		"""Duplicate phone is not added twice to Recipient List or Broadcast."""
		rule_id = f"MA-TEST-{frappe.generate_hash(length=6)}"
		auto = self._make_automation(name=rule_id, title="Dedup Test")
		step = self._make_step()
		lead = self._make_lead(name="LEAD-DUP", mobile_no="+91 98765 43210", lead_name="Charlie")

		with patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}):
			res1 = channels.dispatch_bulk(auto, step, lead, "+91 98765 43210", {})
			self.created_broadcasts.append(res1.get("broadcast"))
			res2 = channels.dispatch_bulk(auto, step, lead, "+91 98765 43210", {})

		bc_doc = frappe.get_doc("Broadcast", res1.get("broadcast"))
		self.created_recipient_lists.append(bc_doc.recipient_list)
		self.assertEqual(bc_doc.total_recipients, 1)
		self.assertEqual(bc_doc.channels[0].recipient_count, 1)

		rec_list = frappe.get_doc("Recipient List", bc_doc.recipient_list)
		self.assertEqual(len(rec_list.contacts), 1)

	def test_opted_out_lead_skipped(self):
		"""Opted-out lead is skipped and not added to list or Broadcast."""
		rule_id = f"MA-TEST-{frappe.generate_hash(length=6)}"
		auto = self._make_automation(name=rule_id, title="Opt-out Test")
		step = self._make_step()
		lead = self._make_lead(mobile_no="+91 98765 43210")

		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}),
			patch(f"{CH}.is_opted_out", return_value=True),
		):
			res = channels.dispatch_bulk(auto, step, lead, "+91 98765 43210", {})
			self.assertEqual(res.get("status"), "skipped")
			self.assertEqual(res.get("reason"), "opted_out")

			added = channels.add_to_list("Any-List", "+91 98765 43210", lead)
			self.assertFalse(added)

	def test_invalid_phone_skipped_with_log_no_exception(self):
		"""Invalid phone number is skipped with a warning log and no exception raised."""
		rule_id = f"MA-TEST-{frappe.generate_hash(length=6)}"
		auto = self._make_automation(name=rule_id, title="Invalid Phone Test")
		step = self._make_step()
		lead = self._make_lead(mobile_no="invalid_not_a_number")

		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}),
			patch("frappe.logger") as mock_logger,
		):
			mock_log_instance = MagicMock()
			mock_logger.return_value = mock_log_instance

			res = channels.dispatch_bulk(auto, step, lead, "invalid_not_a_number", {})
			self.assertEqual(res.get("status"), "skipped")
			self.assertEqual(res.get("reason"), "invalid_phone")
			mock_log_instance.warning.assert_called()

			added = channels.add_to_list("Any-List", "invalid_not_a_number", lead)
			self.assertFalse(added)
