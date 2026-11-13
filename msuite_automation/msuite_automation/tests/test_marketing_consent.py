"""AUT-119: MARKETING templates need a recorded WhatsApp opt-in (setting require_marketing_opt_in)."""

import unittest
from unittest.mock import patch

import frappe
from msuite_automation.msuite_automation.constants import (
	RESULT_SKIPPED_NO_CONSENT,
	RESULT_SKIPPED_OPTED_OUT,
	SEND_WHATSAPP,
)
from msuite_automation.msuite_automation.engine import preview, steps
from msuite_whatsapp.services.channels import automation_channel

CH = "msuite_automation.msuite_automation.engine.channels"
BS = "msuite_whatsapp.services.bulk.bulk_send_service"
PHONE = "+919876543210"


def _ctx():
	return frappe._dict(
		automation=frappe._dict(name="a", send_frequency="Every time", phone_field="phone", whatsapp_account="W1"),
		doc=frappe._dict(doctype="ToDo", name="d"),
		phone=PHONE,
	)


def _step():
	return frappe._dict(step_id="s1", step_type=SEND_WHATSAPP, template="tpl", delivery_mode="Direct")


def _category_lookup(category):
	"""frappe.db.get_value stub: the template category for WhatsApp Templates, nothing else."""
	return lambda doctype, *a, **kw: category if doctype == "WhatsApp Templates" else None


class TestMarketingConsent(unittest.TestCase):
	def _run(self, category="MARKETING", setting=1, consent=False, opted_out=False):
		"""Run steps.execute with every external lookup mocked; returns (result, send mock)."""
		with patch(f"{CH}.already_sent", return_value=False), \
		     patch("frappe.db.get_value", side_effect=_category_lookup(category)), \
		     patch("frappe.get_meta", return_value=frappe._dict(has_field=lambda f: True)), \
		     patch("frappe.db.get_single_value", return_value=setting), \
		     patch("frappe.db.exists", return_value=False), \
		     patch("frappe.db.get_all", return_value=[]), \
		     patch(f"{CH}.whatsapp_opted_out", return_value=opted_out), \
		     patch(f"{CH}.whatsapp_has_consent", return_value=consent), \
		     patch(f"{CH}.send_to_phone", return_value={"ok": True}) as send, \
		     patch(f"{CH}.build_whatsapp", return_value={"to": PHONE, "account": "W1", "payload": {}}), \
		     patch(f"{CH}.claim_for_account", return_value=True):
			return steps.execute(_ctx(), _step()), send

	def test_marketing_without_consent_skipped(self):
		res, send = self._run(consent=False)
		self.assertEqual(res, RESULT_SKIPPED_NO_CONSENT)
		self.assertEqual(res, "skipped: no consent")
		send.assert_not_called()

	def test_marketing_with_consent_sends(self):
		res, send = self._run(consent=True)
		self.assertEqual(res, "sent")
		send.assert_called_once()

	def test_opt_out_wins_over_consent(self):
		res, send = self._run(consent=True, opted_out=True)
		self.assertEqual(res, RESULT_SKIPPED_OPTED_OUT)
		send.assert_not_called()

	def test_utility_without_consent_sends(self):
		res, send = self._run(category="UTILITY", consent=False)
		self.assertEqual(res, "sent")
		send.assert_called_once()

	def test_setting_off_only_checks_opt_out(self):
		res, send = self._run(setting=0, consent=False)
		self.assertEqual(res, "sent")
		res, _ = self._run(setting=0, consent=False, opted_out=True)
		self.assertEqual(res, RESULT_SKIPPED_OPTED_OUT)

	def test_setting_field_missing_counts_as_on_without_reading_it(self):
		"""Before migrate the field is not in the meta: treat as on, never call get_single_value (no message_log noise)."""
		meta = frappe._dict(has_field=lambda f: False)
		with patch("frappe.db.get_value", side_effect=_category_lookup("MARKETING")), \
		     patch("frappe.get_meta", return_value=meta), \
		     patch("frappe.db.get_single_value", side_effect=frappe.ValidationError("no field")) as read, \
		     patch(f"{CH}.whatsapp_has_consent", return_value=False):
			self.assertTrue(steps._lacks_marketing_consent(_ctx(), _step()))
		self.assertNotIn("WhatsApp Settings", [c.args[0] for c in read.call_args_list if c.args])

	def test_setting_zero_means_off(self):
		"""Frappe returns 0 for a Check with no Singles row; the migrate patch writes 1 so 0 is always an explicit choice."""
		res, send = self._run(setting=0, consent=False)
		self.assertEqual(res, "sent")

	def test_scheduled_contact_list_skips_no_consent_quietly(self):
		"""run_scheduled legacy _contact_list path: RecipientNoConsent is a skip, not a logged error."""
		from msuite_automation.msuite_automation.engine import channels, executor
		trigger = frappe._dict(trigger_id="t1", trigger_type="Schedule", reference_doctype="ToDo", condition="x")
		automation = frappe._dict(name="A", triggers=[trigger], steps=[], edges=[], send_frequency="Every time", as_dict=lambda: {})
		step = frappe._dict(step_id="s", step_type=SEND_WHATSAPP)
		graph = type("G", (), {"steps": {"s": step}, "targets": lambda self, t: ["s"]})()

		def fake_exec(code, glob, scope):
			scope.doc["_contact_list"] = ["+919876543210"]

		with patch("frappe.get_doc", return_value=automation), \
		     patch.object(executor, "trigger_row", return_value=trigger), \
		     patch.object(executor, "safe_exec", side_effect=fake_exec), \
		     patch.object(executor, "get_safe_globals", return_value={}), \
		     patch.object(executor, "Graph", return_value=graph), \
		     patch.object(executor, "effective_automation", side_effect=lambda a, *_: a), \
		     patch(f"{CH}.format_number", side_effect=lambda p: p), \
		     patch(f"{CH}.send_whatsapp", side_effect=channels.RecipientNoConsent("x")), \
		     patch("frappe.log_error") as log_error:
			executor.run_scheduled("A", trigger_id="t1")
		log_error.assert_not_called()

	def test_dry_run_shows_no_consent(self):
		with patch("frappe.db.get_value", side_effect=_category_lookup("MARKETING")), \
		     patch(f"{CH}.resolve_phone", return_value=PHONE), \
		     patch("frappe.get_meta", return_value=frappe._dict(has_field=lambda f: True)), \
		     patch("frappe.db.get_single_value", return_value=1), \
		     patch(f"{CH}.whatsapp_has_consent", return_value=False), \
		     patch.object(steps, "_is_marketing_opted_out", return_value=False), \
		     patch.object(preview, "effective_automation", side_effect=lambda a, *_: a):
			out = preview._preview(_ctx().automation, _step(), _ctx().doc | {"doctype": "ToDo"})
		self.assertEqual(out, {"skipped": "no consent"})

	def test_send_whatsapp_path_raises_no_consent(self):
		"""The channels.send_whatsapp guard maps to the same skip result via _send."""
		with patch(f"{CH}.build_whatsapp", return_value={"to": PHONE, "account": "W1", "payload": {}}), \
		     patch.object(steps, "_is_marketing_opted_out", return_value=False), \
		     patch.object(steps, "_lacks_marketing_consent", return_value=True), \
		     patch(f"{CH}.send_to_phone") as send:
			from msuite_automation.msuite_automation.engine import channels
			with self.assertRaises(channels.RecipientNoConsent):
				channels.send_whatsapp(_ctx().automation, _step(), _ctx().doc, phone=PHONE)
			send.assert_not_called()


class TestHasMarketingConsent(unittest.TestCase):
	"""Lookup rule: Channel Handle opted_in, or Contact not unsubscribed with consent_updated_at."""

	def _exists(self, matches):
		def exists(doctype, filters=None, *a, **kw):
			return any(doctype == dt and field in filters for dt, field in matches)
		return exists

	def _check(self, matches, phone=PHONE, opted_out=False):
		with patch.object(automation_channel, "is_opted_out", return_value=opted_out), \
		     patch("frappe.db.exists", side_effect=self._exists(matches)):
			return automation_channel.has_marketing_consent(phone)

	def test_no_records(self):
		self.assertFalse(self._check([]))

	def test_empty_phone(self):
		self.assertFalse(automation_channel.has_marketing_consent(""))

	def test_handle_opted_in_by_phone(self):
		self.assertTrue(self._check([("Channel Handle", "platform_handle")]))

	def test_handle_opted_in_by_bsuid(self):
		self.assertTrue(self._check([("Channel Handle", "bsuid")], phone="IN.1234567890"))

	def test_contact_consent_recorded(self):
		self.assertTrue(self._check([("Contact", "phone_e164")]))
		self.assertTrue(self._check([("Contact", "mobile_no")]))
		self.assertTrue(self._check([("Contact", "phone")]))

	def test_contact_filter_requires_subscribed_and_consent_date(self):
		seen = []
		with patch.object(automation_channel, "is_opted_out", return_value=False), \
		     patch("frappe.db.exists", side_effect=lambda dt, f=None, *a, **k: seen.append((dt, f)) or False):
			automation_channel.has_marketing_consent(PHONE)
		contact_filters = [f for dt, f in seen if dt == "Contact"]
		self.assertTrue(contact_filters)
		for f in contact_filters:
			self.assertEqual(f["unsubscribed_whatsapp"], 0)
			self.assertEqual(f["consent_updated_at"], ["is", "set"])

	def test_bsuid_is_not_digit_stripped(self):
		"""A BSUID must only match itself, never another person's phone handle (IN.919876543210 vs +919876543210)."""
		seen = []
		with patch.object(automation_channel, "is_opted_out", return_value=False), \
		     patch("frappe.db.exists", side_effect=lambda dt, f=None, *a, **k: seen.append(f) or False):
			automation_channel.has_marketing_consent("IN.919876543210")
		for f in seen:
			vals = [v for k, v in f.items() if isinstance(v, list) and v and v[0] == "in"]
			for v in vals:
				self.assertEqual(v[1], ["IN.919876543210"])

	def test_opt_out_wins(self):
		self.assertFalse(self._check([("Channel Handle", "platform_handle")], opted_out=True))
