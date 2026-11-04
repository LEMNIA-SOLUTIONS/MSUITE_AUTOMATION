# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

from __future__ import annotations

import json
import logging
import unittest
from unittest.mock import MagicMock, patch, call

import frappe

# Setup fallback mocks for frappe when running without live site context
_orig_logger = getattr(frappe, "logger", None)
_orig_log_error = getattr(frappe, "log_error", None)

if not getattr(frappe.local, "site", None):
	frappe.logger = lambda *args, **kwargs: logging.getLogger("frappe_test")
	frappe.log_error = MagicMock()

if not getattr(frappe.local, "db", None):
	frappe.local.db = MagicMock()
if not getattr(frappe.local, "session", None):
	frappe.local.session = MagicMock()
if not getattr(frappe.local, "flags", None):
	frappe.local.flags = frappe._dict(mute_messages=True, in_patch=False)


class FakeCache:
	def __init__(self, prefix="site"):
		self.prefix = prefix
		self.store = {}

	def __call__(self):
		return self

	def make_key(self, k, *args, **kwargs):
		if isinstance(k, bytes):
			return k
		return f"{self.prefix}|{k}".encode()

	def get_value(self, k, default=None, **kwargs):
		generator = kwargs.get("generator", default)
		key = self.make_key(k)
		val = self.store.get(key)
		if val is None and callable(generator):
			val = generator()
			self.store[key] = val
			return val
		return val if val is not None else generator

	def set_value(self, k, v, **kwargs):
		key = self.make_key(k)
		self.store[key] = v

	def delete_value(self, k):
		key = self.make_key(k)
		self.store.pop(key, None)

	def set(self, name, value, nx=False, ex=None):
		if nx and name in self.store:
			return None
		self.store[name] = value
		return True

	def delete(self, name):
		self.store.pop(name, None)

	def setnx(self, name, value):
		if name in self.store:
			return False
		self.store[name] = value
		return True

	def expire(self, name, seconds):
		pass

	def hget(self, name, key, generator=None):
		return {}

	def hset(self, name, key, value):
		pass


class BaseWorkflowTestCase(unittest.TestCase):
	"""Base test case that manages mock lifecycle via setUp/tearDown to avoid global test pollution."""

	def setUp(self):
		super().setUp()
		self._orig_local_cache = getattr(frappe.local, "cache", {})
		self.fake_cache = FakeCache()
		self._base_patches = [
			patch.object(frappe, "cache", self.fake_cache, create=True),
			patch.object(frappe, "client_cache", self.fake_cache, create=True),
			patch.object(frappe, "logger", lambda *args, **kwargs: logging.getLogger("frappe_test")),
			patch.object(frappe, "log_error", MagicMock()),
		]
		if hasattr(frappe, "local"):
			self._base_patches.append(patch.object(frappe.local, "cache", self.fake_cache, create=True))

		def _mock_get_meta(doctype):
			meta = MagicMock()
			def _get_field(fieldname):
				if fieldname in ("non_existent_field", "does_not_exist", "invalid_field", "unknown_field", "bad_field"):
					return None
				if fieldname == "password_field":
					return MagicMock(fieldname=fieldname, fieldtype="Password", options="")
				if fieldname in ("custom_shipping_fee", "currency_field"):
					return MagicMock(fieldname=fieldname, fieldtype="Currency", options="")
				if fieldname == "float_field":
					return MagicMock(fieldname=fieldname, fieldtype="Float", options="")
				if fieldname == "int_field":
					return MagicMock(fieldname=fieldname, fieldtype="Int", options="")
				return MagicMock(fieldname=fieldname, fieldtype="Data", options="")
			meta.get_field.side_effect = _get_field
			return meta

		self._base_patches.append(patch.object(frappe, "get_meta", side_effect=_mock_get_meta))
		for p in self._base_patches:
			p.start()

	def tearDown(self):
		for p in reversed(self._base_patches):
			p.stop()
		if hasattr(frappe, "local") and not hasattr(frappe.local, "cache"):
			frappe.local.cache = self._orig_local_cache if self._orig_local_cache is not None else {}
		super().tearDown()


from msuite_core.patches import migrate_order_status_notifications_to_workflows


class TestOrderAutomationWorkflowLegacyMigration(BaseWorkflowTestCase):
	"""8. Legacy WhatsApp Notification to Order Automation Workflow migration patch tests."""

	@patch("frappe.db.commit")
	@patch("msuite_automation.msuite_automation.engine.triggers.clear_cache")
	@patch("frappe.db.set_value")
	@patch("frappe.get_doc")
	@patch("frappe.db.get_value")
	@patch("frappe.db.exists")
	@patch("frappe.get_all")
	@patch("frappe.db.table_exists", return_value=True)
	def test_migration_converts_legacy_and_is_idempotent(
		self, mock_table, mock_get_all, mock_exists, mock_get_val, mock_get_doc, mock_set_val, mock_clear_cache, mock_commit
	):
		mock_get_all.return_value = [
			{
				"name": "Legacy Rule 1",
				"notification_name": "Legacy Rule 1",
				"reference_doctype": "Sales Order",
				"disabled": 0,
				"watched_field": "carrier_tracking_status",
				"target_value": "OUT_FOR_DELIVERY",
				"send_whatsapp": 1,
				"template": "out_for_delivery_tpl",
				"field_name": "contact_mobile",
				"template_variables": '[{"field": "customer_name"}]',
				"send_email": 1,
				"email_field_name": "contact_email",
				"email_subject": "Out for delivery",
				"email_body": "Your order is out for delivery",
				"send_sms": 0,
				"sms_account": "",
				"sms_message": "",
				"whatsapp_account": "WA-1",
				"set_property_after_alert": "status",
				"property_value": "In Transit",
			}
		]

		# 1st run: does not exist -> creates workflow and disables legacy row
		mock_exists.return_value = False
		mock_get_val.return_value = None
		mock_inserted_doc = MagicMock()
		mock_get_doc.return_value = mock_inserted_doc

		migrate_order_status_notifications_to_workflows.execute()

		mock_get_doc.assert_called_once()
		created_doc_args = mock_get_doc.call_args[0][0]
		self.assertEqual(created_doc_args["doctype"], "Order Automation Workflow")
		self.assertEqual(created_doc_args["workflow_name"], "Legacy Rule 1")
		self.assertEqual(created_doc_args["include_return_notes"], 0)

		# Finding 9: Explicit commit removed from migration patch
		mock_commit.assert_not_called()

		# Check canonical linear graph was built
		canvas = json.loads(created_doc_args["canvas_data"])
		self.assertEqual(len(canvas["nodes"]), 4)  # source, status, channel, property
		self.assertEqual(len(canvas["edges"]), 3)

		# Legacy row disabled
		mock_set_val.assert_called_with("WhatsApp Notification", "Legacy Rule 1", "disabled", 1, update_modified=False)

		# 2nd run: already exists -> idempotent, does NOT create again
		mock_get_doc.reset_mock()
		mock_exists.return_value = True
		mock_get_val.return_value = "OAW-001"

		migrate_order_status_notifications_to_workflows.execute()
		mock_get_doc.assert_not_called()

	@patch("frappe.get_doc")
	@patch("frappe.db.exists", return_value=False)
	@patch("frappe.get_all")
	@patch("frappe.db.table_exists", return_value=True)
	def test_migration_skips_invalid_reference_doctype(self, mock_table, mock_get_all, mock_exists, mock_get_doc):
		# Finding 9: Legacy notifications with non-order doctypes are skipped
		mock_get_all.return_value = [
			{
				"name": "Legacy Rule Lead",
				"notification_name": "Legacy Rule Lead",
				"reference_doctype": "Lead",
				"disabled": 0,
			}
		]
		migrate_order_status_notifications_to_workflows.execute()
		mock_get_doc.assert_not_called()

	@patch("frappe.db.set_value")
	@patch("frappe.get_doc")
	@patch("frappe.db.get_value", return_value=None)
	@patch("frappe.db.exists", return_value=False)
	@patch("frappe.get_all")
	@patch("frappe.db.table_exists", return_value=True)
	def test_migration_skips_legacy_with_blank_target_value(self, mock_table, mock_get_all, mock_exists, mock_get_val, mock_get_doc, mock_set_val):
		mock_get_all.return_value = [
			{
				"name": "Legacy Invalid",
				"notification_name": "Legacy Invalid",
				"reference_doctype": "Sales Order",
				"disabled": 0,
				"watched_field": "status",
				"target_value": "",
			}
		]
		migrate_order_status_notifications_to_workflows.execute()
		mock_get_doc.assert_not_called()
		mock_set_val.assert_called_with("WhatsApp Notification", "Legacy Invalid", "disabled", 1, update_modified=False)

	@patch("frappe.db.set_value")
	@patch("frappe.get_doc")
	@patch("frappe.db.get_value", return_value=None)
	@patch("frappe.db.exists")
	@patch("frappe.get_all")
	@patch("frappe.db.table_exists", return_value=True)
	def test_migration_cross_doctype_name_collision_disambiguates(
		self, mock_table, mock_get_all, mock_exists, mock_get_val, mock_get_doc, mock_set_val
	):
		# Finding 1 & name-collision: two legacy notifications with same name for different doctypes
		mock_get_all.return_value = [
			{
				"name": "WN-001",
				"notification_name": "Order Cancelled",
				"reference_doctype": "Sales Order",
				"disabled": 0,
				"watched_field": "status",
				"target_value": "Cancelled",
			},
			{
				"name": "WN-002",
				"notification_name": "Order Cancelled",
				"reference_doctype": "Delivery Note",
				"disabled": 0,
				"watched_field": "status",
				"target_value": "Cancelled",
			},
		]
		created_names = []

		def fake_get_doc(doc_dict):
			m = MagicMock()
			created_names.append(doc_dict.get("workflow_name"))
			return m

		mock_get_doc.side_effect = fake_get_doc

		def fake_exists(dt, filters=None):
			if dt == "Order Automation Workflow" and isinstance(filters, dict):
				if filters.get("workflow_name") == "Order Cancelled" and "Order Cancelled" in created_names:
					return True
			return False

		mock_exists.side_effect = fake_exists

		migrate_order_status_notifications_to_workflows.execute()

		self.assertEqual(len(created_names), 2)
		self.assertEqual(created_names[0], "Order Cancelled")
		self.assertEqual(created_names[1], "Order Cancelled (Delivery Note)")

	@patch("frappe.db.set_value")
	@patch("frappe.get_doc")
	@patch("frappe.db.get_value", return_value=None)
	@patch("frappe.db.exists")
	@patch("frappe.get_all")
	@patch("frappe.db.table_exists", return_value=True)
	def test_migration_disambiguates_when_name_already_exists(
		self, mock_table, mock_get_all, mock_exists, mock_get_val, mock_get_doc, mock_set_val
	):
		# Name collision finding: disambiguates with #2 if base disambiguated name also exists
		mock_get_all.return_value = [
			{
				"name": "WN-003",
				"notification_name": "Order Dispatched",
				"reference_doctype": "Delivery Note",
				"disabled": 0,
				"watched_field": "carrier_tracking_status",
				"target_value": "DISPATCHED",
			}
		]
		created_names = []
		mock_get_doc.side_effect = lambda doc_dict: (created_names.append(doc_dict.get("workflow_name")), MagicMock())[1]

		# Both "Order Dispatched" and "Order Dispatched (Delivery Note)" exist
		def fake_exists(dt, filters=None):
			if dt == "Order Automation Workflow":
				if isinstance(filters, dict):
					name = filters.get("workflow_name")
					if name in ("Order Dispatched", "Order Dispatched (Delivery Note)"):
						return True
				elif filters in ("Order Dispatched", "Order Dispatched (Delivery Note)"):
					return True
			return False

		mock_exists.side_effect = fake_exists

		migrate_order_status_notifications_to_workflows.execute()

		self.assertEqual(len(created_names), 1)
		self.assertEqual(created_names[0], "Order Dispatched (Delivery Note) #2")


if __name__ == "__main__":
	unittest.main()
