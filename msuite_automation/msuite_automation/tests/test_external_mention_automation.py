"""An external, non-admin API user can manage Instagram mention automations; others cannot."""

import json
import unittest
from unittest.mock import patch

import frappe

from msuite_core.constants import SOCIAL_MENTION_DOCTYPE
from msuite_core.services.permissions.roles import VIEWER
from msuite_automation.msuite_automation.api import automations as api
from msuite_automation.msuite_automation.constants import RUN_DOCTYPE, RUN_LIST_MAX_LIMIT, SEND_IG_PUBLIC_REPLY

API_ROLE = "WhatsApp Manager"  # role that can write Messaging Automation (see its DocType permissions)
SOCIAL_ONLY_ROLE = "Social Manager"  # an MSuite role with no Messaging Automation permission


def _user(role: str) -> str:
	email = f"ext-mention-{role.lower().replace(' ', '-')}@example.com"
	if not frappe.db.exists("User", email):
		frappe.get_doc({"doctype": "User", "email": email, "first_name": "Ext", "send_welcome_email": 0}).insert(
			ignore_permissions=True
		).add_roles(role)
	return email


def _mention_payload(**kw) -> dict:
	return {
		"title": "ZZ ext mention", "disabled": 1,
		"triggers": [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": SOCIAL_MENTION_DOCTYPE, "doc_event": "After Insert"}],
		"steps": [{"step_id": "s1", "step_type": SEND_IG_PUBLIC_REPLY, "ig_reply_text": "Thanks for the mention!"}],
		"edges": [{"source_id": "t1", "source_handle": "", "target_id": "s1"}],
		**kw,
	}


class TestExternalMentionAutomation(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.set_user("Administrator")
		frappe.db.rollback()

	def _create_as_admin(self) -> str:
		return api.save_automation(_mention_payload())["name"]

	def test_api_role_can_create_and_toggle_mention_automation(self):
		user = _user(API_ROLE)
		frappe.set_user(user)
		name = api.save_automation(json.dumps(_mention_payload()))["name"]  # JSON string, as sent over HTTP
		self.assertEqual(api.get_automation(name)["steps"][0]["ig_reply_text"], "Thanks for the mention!")
		self.assertEqual(api.toggle_automation(name, 1)["disabled"], 0)
		self.assertEqual(api.toggle_automation(name, 0)["disabled"], 1)

	def test_viewer_cannot_save_or_toggle(self):
		name = self._create_as_admin()
		frappe.set_user(_user(VIEWER))
		with self.assertRaises(frappe.PermissionError):
			api.save_automation(_mention_payload(title="ZZ viewer"))
		with self.assertRaises(frappe.PermissionError):
			api.toggle_automation(name, 1)

	def test_guest_denied(self):
		name = self._create_as_admin()
		frappe.set_user("Guest")
		for call in (lambda: api.save_automation(_mention_payload()), lambda: api.toggle_automation(name, 1), lambda: api.get_runs(name)):
			with self.assertRaises(frappe.PermissionError):
				call()

	def test_toggle_nonexistent_raises_not_found(self):
		frappe.set_user(_user(API_ROLE))
		with self.assertRaises(frappe.DoesNotExistError):
			api.toggle_automation("NO-SUCH-AUTOMATION", 1)

	def test_get_runs_limit_is_capped(self):
		name = self._create_as_admin()
		real = frappe.get_all
		with patch("frappe.get_all", wraps=real) as spy:
			api.get_runs(name, limit=10**6)
		run_calls = [c for c in spy.call_args_list if c.args and c.args[0] == RUN_DOCTYPE]
		self.assertEqual(run_calls[0].kwargs["limit"], RUN_LIST_MAX_LIMIT)

	def test_get_runs_denied_without_messaging_automation_permission(self):
		name = self._create_as_admin()
		frappe.set_user(_user(SOCIAL_ONLY_ROLE))
		with self.assertRaises(frappe.PermissionError):
			api.get_runs(name)

	def test_save_with_invalid_trigger_type_rejected(self):
		frappe.set_user(_user(API_ROLE))
		payload = _mention_payload()
		payload["triggers"][0]["trigger_type"] = "Bogus"
		with self.assertRaises(frappe.ValidationError):
			api.save_automation(payload)
