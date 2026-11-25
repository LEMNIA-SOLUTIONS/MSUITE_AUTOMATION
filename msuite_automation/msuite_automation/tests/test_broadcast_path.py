# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for lead status automation Broadcast path gated by msuite_broadcast_enabled."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.utils import add_to_date, now_datetime

from msuite_automation.msuite_automation.engine import channels

CH = "msuite_automation.msuite_automation.engine.channels"


class TestBroadcastPath(unittest.TestCase):
	"""Test suite for unified Recipient List and Broadcast lead status automation."""

	def setUp(self):
		frappe.db.rollback()
		self.created_broadcasts: list[str] = []
		self.created_recipient_lists: list[str] = []
		self.created_bulk_messages: list[str] = []
		self.created_legacy_lists: list[str] = []

	def tearDown(self):
		frappe.db.rollback()
		for name in self.created_broadcasts:
			if frappe.db.exists("Broadcast", name):
				frappe.delete_doc("Broadcast", name, force=True)
		for name in self.created_recipient_lists:
			if frappe.db.exists("Recipient List", name):
				frappe.delete_doc("Recipient List", name, force=True)
		for name in self.created_bulk_messages:
			if frappe.db.exists("DocType", "Bulk WhatsApp Message") and frappe.db.exists("Bulk WhatsApp Message", name):
				frappe.delete_doc("Bulk WhatsApp Message", name, force=True)
		for name in self.created_legacy_lists:
			if frappe.db.exists("DocType", "WhatsApp Recipient List") and frappe.db.exists("WhatsApp Recipient List", name):
				frappe.delete_doc("WhatsApp Recipient List", name, force=True)
		frappe.db.commit()

	def _make_automation(self, name: str = "MA-TEST-RULE-1", title: str = "Test Rule"):
		return frappe._dict(
			name=name,
			title=title,
			whatsapp_account="WA-ACC-001",
			_trigger_label="Status",
		)

	def _make_step(
		self,
		template: str = "hello_template",
		recipient_list: str | None = None,
		bulk_schedule_type: str = "Immediate",
		bulk_delay_minutes: int = 0,
		bulk_scheduled_time=None,
		target_broadcast: str | None = None,
	):
		return frappe._dict(
			step_id="wa_step_1",
			template=template,
			recipient_list=recipient_list,
			bulk_schedule_type=bulk_schedule_type,
			bulk_delay_minutes=bulk_delay_minutes,
			bulk_scheduled_time=bulk_scheduled_time,
			target_broadcast=target_broadcast,
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

	@unittest.skipUnless(frappe.db.exists("DocType", "WhatsApp Recipient List"), "legacy doctype deleted")
	def test_flag_off_calls_old_path_not_new(self):
		"""With msuite_broadcast_enabled falsy, runs REAL legacy functions with mocked network boundary (I-05b)."""
		legacy_list_name = f"Legacy-List-{frappe.generate_hash(length=6)}"
		legacy_list_doc = frappe.get_doc(
			{
				"doctype": "WhatsApp Recipient List",
				"list_name": legacy_list_name,
			}
		)
		legacy_list_doc.insert(ignore_permissions=True, ignore_links=True)
		self.created_legacy_lists.append(legacy_list_name)

		wa_account = frappe.db.get_value("WhatsApp Account", {}, "name")
		wa_template = frappe.db.get_value("WhatsApp Templates", {}, "name")
		ma_rule = frappe.db.get_value("Messaging Automation", {}, "name")

		auto = self._make_automation(name=ma_rule, title="Legacy Rule")
		auto.whatsapp_account = wa_account
		step = self._make_step(
			template=wa_template,
			recipient_list=legacy_list_name,
			bulk_schedule_type="Immediate",
		)
		lead = self._make_lead(mobile_no="+91 98765 43210")

		# 1. Test add_to_list runs real _add_to_legacy_list
		with patch.dict(frappe.conf, {"msuite_broadcast_enabled": False}):
			added = channels.add_to_list(legacy_list_name, "+919876543210", lead)
			self.assertTrue(added)
			self.assertTrue(
				frappe.db.exists(
					"WhatsApp Recipient",
					{"parent": legacy_list_name, "mobile_number": "+919876543210"},
				)
			)
			# Duplicate add returns False
			added_dup = channels.add_to_list(legacy_list_name, "+919876543210", lead)
			self.assertFalse(added_dup)

		# 2. Test dispatch_bulk runs real _dispatch_bulk_legacy (mocking only the outbound queue call)
		mock_wa_app = MagicMock()
		mock_wa_app.queue_bulk = MagicMock()
		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": False}),
			patch(f"{CH}._require_channel_app", return_value=mock_wa_app),
		):
			res = channels.dispatch_bulk(auto, step, lead, "+919876543210", {"1": "Val"})
			self.assertEqual(res.get("status"), "success")
			self.assertEqual(res.get("mode"), "created")
			bulk_name = res.get("bulk_message")
			self.assertTrue(bulk_name)
			self.created_bulk_messages.append(bulk_name)

			# Verify real DB records created
			self.assertTrue(frappe.db.exists("Bulk WhatsApp Message", bulk_name))
			bulk_doc = frappe.get_doc("Bulk WhatsApp Message", bulk_name)
			self.assertEqual(bulk_doc.status, "In Progress")
			self.assertEqual(bulk_doc.recipient_list, legacy_list_name)
			mock_wa_app.queue_bulk.assert_called_once()

			# Ensure no Broadcast doc was created
			self.assertFalse(frappe.db.exists("Broadcast", {"title": ["like", f"%{auto.name}%"]}))

	def test_channels_tolerates_deleted_legacy_doctypes(self):
		"""Verify channels functions tolerate deleted legacy DocTypes without raising DoesNotExistError."""
		lead = self._make_lead(mobile_no="+91 98765 43210")
		self.assertFalse(channels.is_opted_out("+919876543210", "Email"))
		self.assertFalse(channels.is_opted_out("+919876543210", "SMS"))
		with patch.dict(frappe.conf, {"msuite_broadcast_enabled": False}):
			self.assertFalse(channels.add_to_list("nonexistent_list", "+919876543210", lead))
			auto = self._make_automation()
			step = self._make_step()
			res = channels.dispatch_bulk(auto, step, lead, "+919876543210", {})
			self.assertEqual(res.get("status"), "error")

	def test_flag_on_immediate_launches_broadcast_and_creates_recipient(self):
		"""With flag on, Immediate creates Individual Broadcast, launches it, and materializes recipient (C-03)."""
		rule_id = f"MA-TEST-{frappe.generate_hash(length=6)}"
		auto = self._make_automation(name=rule_id, title="Immediate Onboarding")
		step = self._make_step(bulk_schedule_type="Immediate")
		lead = self._make_lead(mobile_no="+91 98765 43210", lead_name="John Doe")

		wa_adapter = {"channel": "WhatsApp", "validate_channel": lambda b, c: None, "send_one": MagicMock()}
		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}),
			patch("msuite_campaigns.broadcast.adapters.require_channel_adapter", return_value=wa_adapter),
			patch("msuite_campaigns.broadcast.enqueue.enqueue_broadcast_channel"),
		):
			res = channels.dispatch_bulk(auto, step, lead, "+91 98765 43210", {"1": "John"})

		self.assertEqual(res.get("status"), "success")
		self.assertEqual(res.get("mode"), "created")
		bc_name = res.get("broadcast")
		self.assertTrue(bc_name)
		self.created_broadcasts.append(bc_name)

		# Verify Broadcast ends in Queued or In Progress
		bc_doc = frappe.get_doc("Broadcast", bc_name)
		self.assertEqual(bc_doc.recipient_source, "Individual")
		self.assertIn(bc_doc.status, ("Queued", "In Progress"))
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
		self.assertIn(ch_row.status, ("Queued", "In Progress"))

		# Verify exactly one Broadcast Recipient exists in DB
		rec_count = frappe.db.count("Broadcast Recipient", {"broadcast": bc_name})
		self.assertEqual(rec_count, 1)
		rec_row = frappe.db.get_value(
			"Broadcast Recipient",
			{"broadcast": bc_name},
			["address", "recipient_name"],
			as_dict=True,
		)
		self.assertEqual(rec_row.address, "+919876543210")
		self.assertEqual(rec_row.recipient_name, "John Doe")

	def test_scheduled_delay_buffer_reaches_scheduled_status_and_aggregates(self):
		"""Delay Buffer creates a Broadcast that reaches Scheduled status and aggregates subsequent leads."""
		rule_id = f"MA-TEST-{frappe.generate_hash(length=6)}"
		auto = self._make_automation(name=rule_id, title="Delay Buffer Rule")
		step = self._make_step(bulk_schedule_type="Delay Buffer", bulk_delay_minutes=30)
		lead1 = self._make_lead(name="LEAD-1", mobile_no="+91 98765 43210", lead_name="Alice")
		lead2 = self._make_lead(name="LEAD-2", mobile_no="+91 98765 43211", lead_name="Bob")

		wa_adapter = {"channel": "WhatsApp", "validate_channel": lambda b, c: None, "send_one": MagicMock()}

		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}),
			patch("msuite_campaigns.broadcast.adapters.require_channel_adapter", return_value=wa_adapter),
			patch("msuite_campaigns.broadcast.adapters.get_channel_adapter", return_value=wa_adapter),
		):
			res1 = channels.dispatch_bulk(auto, step, lead1, "+91 98765 43210", {})
			self.assertEqual(res1.get("status"), "success")
			self.assertEqual(res1.get("mode"), "created")
			bc1 = res1.get("broadcast")
			self.created_broadcasts.append(bc1)

			# Verify reaches Scheduled status
			bc_doc = frappe.get_doc("Broadcast", bc1)
			self.created_recipient_lists.append(bc_doc.recipient_list)
			self.assertEqual(bc_doc.recipient_source, "Recipient List")
			self.assertEqual(bc_doc.status, "Scheduled")
			self.assertEqual(bc_doc.channels[0].status, "Scheduled")
			self.assertIsNotNone(bc_doc.scheduled_time)
			self.assertEqual(bc_doc.total_recipients, 1)

			# Verify Broadcast Recipient is materialized
			rec_rows_1 = frappe.get_all("Broadcast Recipient", filters={"broadcast": bc1}, pluck="address")
			self.assertEqual(rec_rows_1, ["+919876543210"])

			# Second lead aggregates
			res2 = channels.dispatch_bulk(auto, step, lead2, "+91 98765 43211", {})
			self.assertEqual(res2.get("status"), "success")
			self.assertEqual(res2.get("mode"), "aggregated")
			self.assertEqual(res2.get("broadcast"), bc1)

		bc_doc.reload()
		self.assertEqual(bc_doc.total_recipients, 2)
		self.assertEqual(bc_doc.channels[0].recipient_count, 2)
		rec_list = frappe.get_doc("Recipient List", bc_doc.recipient_list)
		self.assertEqual(len(rec_list.contacts), 2)

		rec_rows_2 = sorted(frappe.get_all("Broadcast Recipient", filters={"broadcast": bc1}, pluck="address"))
		self.assertEqual(rec_rows_2, ["+919876543210", "+919876543211"])

	def test_scheduled_time_lead_automation_materializes_and_scheduler_dispatches(self):
		"""Scheduled Time rule materializes lead 1 and lead 2, aggregates them, and scheduler dispatches to adapter."""
		rule_id = f"MA-TEST-{frappe.generate_hash(length=6)}"
		auto = self._make_automation(name=rule_id, title="Scheduled Rule")
		scheduled_time = add_to_date(now_datetime(), hours=2)
		step = self._make_step(
			bulk_schedule_type="Scheduled Time",
			bulk_scheduled_time=scheduled_time,
		)
		lead1 = self._make_lead(name="LEAD-1", mobile_no="+91 98765 43210", lead_name="Alice")
		lead2 = self._make_lead(name="LEAD-2", mobile_no="+91 98765 43211", lead_name="Bob")

		wa_adapter = {
			"channel": "WhatsApp",
			"validate_channel": lambda b, c: None,
			"send_one": MagicMock(return_value={"status": "Sent", "message_id": "MSG-001"}),
		}

		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}),
			patch("msuite_campaigns.broadcast.adapters.require_channel_adapter", return_value=wa_adapter),
			patch("msuite_campaigns.broadcast.adapters.get_channel_adapter", return_value=wa_adapter),
		):
			# 1. Lead 1 dispatched
			res1 = channels.dispatch_bulk(auto, step, lead1, "+91 98765 43210", {})
			self.assertEqual(res1.get("status"), "success")
			self.assertEqual(res1.get("mode"), "created")
			bc_name = res1.get("broadcast")
			self.created_broadcasts.append(bc_name)

			# Assert after Lead 1
			bc_doc = frappe.get_doc("Broadcast", bc_name)
			self.created_recipient_lists.append(bc_doc.recipient_list)
			self.assertEqual(bc_doc.status, "Scheduled")
			self.assertEqual(bc_doc.channels[0].status, "Scheduled")
			self.assertEqual(bc_doc.channels[0].recipient_count, 1)
			self.assertEqual(bc_doc.total_recipients, 1)

			rec_addrs_1 = frappe.get_all("Broadcast Recipient", filters={"broadcast": bc_name}, pluck="address")
			self.assertEqual(rec_addrs_1, ["+919876543210"])

			# 2. Lead 2 dispatched (aggregates to the same broadcast)
			res2 = channels.dispatch_bulk(auto, step, lead2, "+91 98765 43211", {})
			self.assertEqual(res2.get("status"), "success")
			self.assertEqual(res2.get("mode"), "aggregated")
			self.assertEqual(res2.get("broadcast"), bc_name)

			# Assert after Lead 2
			bc_doc.reload()
			self.assertEqual(bc_doc.status, "Scheduled")
			self.assertEqual(bc_doc.channels[0].status, "Scheduled")
			self.assertEqual(bc_doc.channels[0].recipient_count, 2)
			self.assertEqual(bc_doc.total_recipients, 2)

			rec_addrs_2 = sorted(frappe.get_all("Broadcast Recipient", filters={"broadcast": bc_name}, pluck="address"))
			self.assertEqual(rec_addrs_2, ["+919876543210", "+919876543211"])

			# 3. Patch clock past scheduled_time and run scheduler
			from msuite_campaigns.broadcast.scheduler import start_due_channels

			def synchronous_enqueue(fn, **kwargs):
				if fn.__name__ == "run_chunk":
					fn(
						broadcast=kwargs["broadcast"],
						channel=kwargs["channel"],
						first_name=kwargs["first_name"],
						last_name=kwargs["last_name"],
					)

			future_now = add_to_date(scheduled_time, minutes=5)
			with (
				patch("msuite_campaigns.broadcast.scheduler.now_datetime", return_value=future_now),
				patch("frappe.enqueue", side_effect=synchronous_enqueue),
			):
				started = start_due_channels()
				self.assertGreaterEqual(started, 1)

			# Channel should now be In Progress
			bc_doc.reload()
			self.assertEqual(bc_doc.channels[0].status, "In Progress")

			# Verify both recipients were handed to the fake adapter
			self.assertEqual(wa_adapter["send_one"].call_count, 2)
			delivered_addresses = sorted(
				call.args[2]["address"] for call in wa_adapter["send_one"].call_args_list
			)
			self.assertEqual(delivered_addresses, ["+919876543210", "+919876543211"])

	def test_scheduled_lead_automation_hub_launch_error_returns_error_status(self):
		"""When hub.launch reports an error for scheduled broadcast, logs and returns legacy error result."""
		rule_id = f"MA-TEST-{frappe.generate_hash(length=6)}"
		auto = self._make_automation(name=rule_id, title="Error Launch Rule")
		scheduled_time = add_to_date(now_datetime(), hours=2)
		step = self._make_step(
			bulk_schedule_type="Scheduled Time",
			bulk_scheduled_time=scheduled_time,
		)
		lead1 = self._make_lead(name="LEAD-1", mobile_no="+91 98765 43210", lead_name="Alice")

		error_res = {
			"WhatsApp": {
				"status": "Draft",
				"queued": 0,
				"dropped": 0,
				"error": "Plan quota exceeded for WhatsApp",
			}
		}

		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}),
			patch("msuite_campaigns.broadcast.hub.launch", return_value=error_res) as mock_launch,
			patch("frappe.logger") as mock_logger,
		):
			mock_log_instance = MagicMock()
			mock_logger.return_value = mock_log_instance

			res = channels.dispatch_bulk(auto, step, lead1, "+91 98765 43210", {})
			self.assertEqual(res.get("status"), "error")
			self.assertIn("launch_error: Plan quota exceeded for WhatsApp", res.get("reason", ""))
			bc_name = res.get("broadcast")
			self.assertTrue(bc_name)
			self.created_broadcasts.append(bc_name)
			mock_log_instance.error.assert_called()

	def test_scheduled_lead_automation_hub_launch_zero_queued_logs_warning(self):
		"""When hub.launch reports queued == 0, logs a warning."""
		rule_id = f"MA-TEST-{frappe.generate_hash(length=6)}"
		auto = self._make_automation(name=rule_id, title="Zero Queued Rule")
		scheduled_time = add_to_date(now_datetime(), hours=2)
		step = self._make_step(
			bulk_schedule_type="Scheduled Time",
			bulk_scheduled_time=scheduled_time,
		)
		lead1 = self._make_lead(name="LEAD-1", mobile_no="+91 98765 43210", lead_name="Alice")

		zero_res = {
			"WhatsApp": {
				"status": "Draft",
				"queued": 0,
				"dropped": 1,
				"error": None,
			}
		}

		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}),
			patch("msuite_campaigns.broadcast.hub.launch", return_value=zero_res),
			patch("frappe.logger") as mock_logger,
		):
			mock_log_instance = MagicMock()
			mock_logger.return_value = mock_log_instance

			res = channels.dispatch_bulk(auto, step, lead1, "+91 98765 43210", {})
			self.assertEqual(res.get("status"), "success")
			bc_name = res.get("broadcast")
			self.assertTrue(bc_name)
			self.created_broadcasts.append(bc_name)
			mock_log_instance.warning.assert_called()

	def test_target_broadcast_non_draft_skipped_with_warning(self):
		"""When target_broadcast is already non-Draft, skips with warning and does not attach contact (M-01)."""
		# Create a target broadcast in Completed / In Progress status
		target_bc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"Target-Non-Draft-{frappe.generate_hash(length=6)}",
				"recipient_source": "Individual",
				"status": "In Progress",
				"channels": [
					{
						"doctype": "Broadcast Channel",
						"channel": "WhatsApp",
						"whatsapp_account": "WA-ACC-001",
						"whatsapp_template": "hello_template",
						"status": "In Progress",
					}
				],
			}
		)
		target_bc.insert(ignore_permissions=True, ignore_links=True)
		self.created_broadcasts.append(target_bc.name)

		auto = self._make_automation()
		step = self._make_step(target_broadcast=target_bc.name)
		lead = self._make_lead(mobile_no="+91 98765 43210")

		with (
			patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}),
			patch("frappe.logger") as mock_logger,
		):
			mock_log_instance = MagicMock()
			mock_logger.return_value = mock_log_instance

			res = channels.dispatch_bulk(auto, step, lead, "+91 98765 43210", {})
			self.assertEqual(res.get("status"), "skipped")
			self.assertEqual(res.get("reason"), "target_broadcast_not_open")
			mock_log_instance.warning.assert_called()

		# Ensure no recipients were materialized or added
		self.assertEqual(frappe.db.count("Broadcast Recipient", {"broadcast": target_bc.name}), 0)

	def test_target_broadcast_draft_attaches_lead(self):
		"""When target_broadcast is Draft, attaches lead to its recipient list and updates counts."""
		rl = frappe.get_doc(
			{
				"doctype": "Recipient List",
				"list_name": f"Target-RL-{frappe.generate_hash(length=6)}",
				"source": "Manual",
			}
		)
		rl.insert(ignore_permissions=True, ignore_links=True)
		self.created_recipient_lists.append(rl.name)

		target_bc = frappe.get_doc(
			{
				"doctype": "Broadcast",
				"title": f"Target-Draft-{frappe.generate_hash(length=6)}",
				"recipient_source": "Recipient List",
				"recipient_list": rl.name,
				"status": "Draft",
				"channels": [
					{
						"doctype": "Broadcast Channel",
						"channel": "WhatsApp",
						"whatsapp_account": "WA-ACC-001",
						"whatsapp_template": "hello_template",
						"status": "Draft",
					}
				],
			}
		)
		target_bc.insert(ignore_permissions=True, ignore_links=True)
		self.created_broadcasts.append(target_bc.name)

		auto = self._make_automation()
		step = self._make_step(target_broadcast=target_bc.name)
		lead = self._make_lead(mobile_no="+91 98765 43210")

		with patch.dict(frappe.conf, {"msuite_broadcast_enabled": True}):
			res = channels.dispatch_bulk(auto, step, lead, "+91 98765 43210", {})
			self.assertEqual(res.get("status"), "success")
			self.assertEqual(res.get("mode"), "target_attached")
			self.assertEqual(res.get("broadcast"), target_bc.name)

		target_bc.reload()
		self.assertEqual(target_bc.total_recipients, 1)
		self.assertEqual(target_bc.channels[0].recipient_count, 1)
		rl.reload()
		self.assertEqual(len(rl.contacts), 1)

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
