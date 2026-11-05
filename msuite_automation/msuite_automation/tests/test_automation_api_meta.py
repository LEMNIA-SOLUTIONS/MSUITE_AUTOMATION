# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for automation builder capabilities and metadata endpoints."""

from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

import frappe

from msuite_core.constants import (
	IG_CONSENT_IMAGE_SOURCE_POST,
	MESSENGER_BUTTON_TITLE_MAX,
	MESSENGER_TEMPLATE_SUBTITLE_MAX,
	MESSENGER_TEMPLATE_TITLE_MAX,
)
from msuite_automation.msuite_automation.constants import (
	DOCTYPE,
	IG_CONSENT_DEFAULT_EXPIRY_DAYS,
	IG_CONSENT_MIN_EXPIRY_DAYS,
)
from msuite_inbox.inbox.constants import MESSENGER_TEMPLATES_FLAG
from msuite_automation.msuite_automation.api import automations as api


class MockMeta:
	"""Lightweight stub delegating get_permlevel_access to frappe.model.meta.Meta."""

	def get_permlevel_access(self, *args, **kwargs):
		return frappe.model.meta.Meta.get_permlevel_access(self, *args, **kwargs)


class TestAutomationApiMeta(unittest.TestCase):
	"""Test get_ig_step_meta and get_condition_edit_access endpoints."""

	def setUp(self):
		"""Set up test environment and reset user to Administrator."""
		frappe.flags = frappe._dict(mute_messages=True, in_test=True)
		if frappe.client_cache is None:
			frappe.client_cache = MagicMock()
		if not hasattr(frappe.local, "session") or frappe.local.session is None:
			frappe.local.session = frappe._dict(user="Administrator")
		if not hasattr(frappe.local, "conf") or frappe.local.conf is None:
			frappe.local.conf = frappe._dict()
		frappe.set_user("Administrator")
		if not getattr(frappe.local, "db", None):
			self._meta_patcher = patch("frappe.get_meta", return_value=MockMeta())
			self._meta_patcher.start()

	def tearDown(self):
		"""Rollback any database transaction and reset user."""
		if hasattr(self, "_meta_patcher"):
			self._meta_patcher.stop()
		if hasattr(frappe.local, "db") and frappe.local.db:
			frappe.db.rollback()
		if hasattr(frappe.local, "session") and frappe.local.session:
			frappe.set_user("Administrator")

	def test_get_ig_step_meta_templates_enabled_true(self):
		"""Verify get_ig_step_meta reflects templates_enabled=True when flag is on."""
		with patch.object(api, "check_read"), patch.dict(frappe.conf, {MESSENGER_TEMPLATES_FLAG: True}):
			meta = api.get_ig_step_meta()
			self.assertTrue(meta["templates_enabled"])
			self.assertEqual(meta["title_max"], MESSENGER_TEMPLATE_TITLE_MAX)
			self.assertEqual(meta["subtitle_max"], MESSENGER_TEMPLATE_SUBTITLE_MAX)
			self.assertEqual(meta["button_title_max"], MESSENGER_BUTTON_TITLE_MAX)
			self.assertEqual(meta["expiry_min"], IG_CONSENT_MIN_EXPIRY_DAYS)
			self.assertEqual(meta["expiry_default"], IG_CONSENT_DEFAULT_EXPIRY_DAYS)
			self.assertEqual(meta["image_source_post"], IG_CONSENT_IMAGE_SOURCE_POST)

	def test_get_ig_step_meta_templates_enabled_false(self):
		"""Verify get_ig_step_meta reflects templates_enabled=False when flag is off."""
		with patch.object(api, "check_read"), patch.dict(frappe.conf, {MESSENGER_TEMPLATES_FLAG: False}):
			meta = api.get_ig_step_meta()
			self.assertFalse(meta["templates_enabled"])

	def test_get_ig_step_meta_guest_rejected(self):
		"""Verify get_ig_step_meta raises PermissionError for Guest user."""
		frappe.set_user("Guest")
		with patch("msuite_core.services.permissions.checks.frappe.has_permission", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				api.get_ig_step_meta()

	def test_get_condition_edit_access_follows_permlevel_write(self):
		"""Verify the endpoint mirrors the permlevel 1 write rule save_automation enforces."""
		cases = (([0, 1], True), ([0], False), ([], False))
		for levels, expected in cases:
			with self.subTest(levels=levels):
				with patch.object(api, "check_read"), \
					patch("frappe.has_permission", return_value=True), \
					patch("frappe.model.meta.Meta.get_permlevel_access", return_value=levels):
					self.assertIs(api.get_condition_edit_access(), expected)

	def test_get_condition_edit_access_allowed_role(self):
		"""Verify get_condition_edit_access returns True for user with allowed write role and permlevel 1."""
		with patch.object(api, "check_read"), \
			patch("frappe.has_permission", return_value=True), \
			patch("frappe.model.meta.Meta.get_permlevel_access", return_value=[0, 1]):
			self.assertTrue(api.get_condition_edit_access())

	def test_get_condition_edit_access_denied_role(self):
		"""Verify get_condition_edit_access returns False when role lacks write or permlevel 1 access."""
		# Subcase 1: Read allowed, but no write access
		with patch.object(api, "check_read"), \
			patch("frappe.has_permission", return_value=False), \
			patch("frappe.model.meta.Meta.get_permlevel_access", return_value=[0, 1]):
			self.assertFalse(api.get_condition_edit_access())

		# Subcase 2: Write allowed, but lacks permlevel 1 write access
		with patch.object(api, "check_read"), \
			patch("frappe.has_permission", return_value=True), \
			patch("frappe.model.meta.Meta.get_permlevel_access", return_value=[0]):
			self.assertFalse(api.get_condition_edit_access())

	def test_get_condition_edit_access_owner(self):
		"""Verify get_condition_edit_access returns True for document owner with permlevel 1 write."""
		doc_name = "AUTO-OWNER-001"
		with patch.object(api, "check_read"), \
			patch("frappe.has_permission", return_value=True) as perm_mock, \
			patch("frappe.model.meta.Meta.get_permlevel_access", return_value=[0, 1]):
			self.assertTrue(api.get_condition_edit_access(name=doc_name))
			perm_mock.assert_called_with(DOCTYPE, "write", doc=doc_name)

	def test_get_condition_edit_access_non_owner(self):
		"""Verify get_condition_edit_access returns False for non-owner without document write permission."""
		doc_name = "AUTO-OTHER-001"
		with patch.object(api, "check_read"), \
			patch("frappe.has_permission", return_value=False) as perm_mock, \
			patch("frappe.model.meta.Meta.get_permlevel_access", return_value=[0, 1]):
			self.assertFalse(api.get_condition_edit_access(name=doc_name))
			perm_mock.assert_called_with(DOCTYPE, "write", doc=doc_name)

	def test_get_condition_edit_access_guest_rejected(self):
		"""Verify get_condition_edit_access raises PermissionError for Guest user."""
		frappe.set_user("Guest")
		with patch("msuite_core.services.permissions.checks.frappe.has_permission", return_value=False):
			with self.assertRaises(frappe.PermissionError):
				api.get_condition_edit_access()
