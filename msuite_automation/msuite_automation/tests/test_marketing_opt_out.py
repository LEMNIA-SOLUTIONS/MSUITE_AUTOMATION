import unittest
from unittest.mock import patch, MagicMock

import frappe
from msuite_automation.msuite_automation.constants import SEND_EMAIL, SEND_SMS, SEND_WHATSAPP
from msuite_automation.msuite_automation.engine import channels, steps

CH = "msuite_automation.msuite_automation.engine.channels"
BS = "msuite_whatsapp.services.bulk.bulk_send_service"


class TestMarketingOptOut(unittest.TestCase):
	def setUp(self):
		self.templates_to_cleanup = []
		self.contacts_to_cleanup = []

	def tearDown(self):
		frappe.db.rollback()
		for t in self.templates_to_cleanup:
			if frappe.db.exists("WhatsApp Templates", t):
				frappe.delete_doc("WhatsApp Templates", t, force=True, ignore_permissions=True)
		for c in self.contacts_to_cleanup:
			if frappe.db.exists("Contact", c):
				frappe.delete_doc("Contact", c, force=True, ignore_permissions=True)

	def _make_template(self, name, category):
		if frappe.db.exists("WhatsApp Templates", name):
			doc = frappe.get_doc("WhatsApp Templates", name)
			doc.category = category
			doc.flags.ignore_permissions = True
			with patch("msuite_whatsapp.services.templates.template_service.push_to_meta"):
				doc.save(ignore_permissions=True)
		else:
			doc = frappe.get_doc({
				"doctype": "WhatsApp Templates",
				"template_name": name,
				"category": category,
				"language": "en",
				"status": "APPROVED",
				"template": "Hello world",
			})
			doc.flags.ignore_permissions = True
			with patch("msuite_whatsapp.services.templates.template_service.push_to_meta"):
				doc.insert(ignore_permissions=True)
			self.templates_to_cleanup.append(doc.name)
		return doc.name

	def test_marketing_template_opted_out_skips(self):
		"""Marketing WhatsApp template sent to an opted-out number returns 'skipped: opted out'."""
		tpl_name = self._make_template("test_promo_tpl", "MARKETING")

		class DummyStep:
			step_id = "s1"
			step_type = SEND_WHATSAPP
			template = tpl_name
			delivery_mode = "Direct"

		class DummyDoc:
			doctype = "ToDo"
			name = "dummy-doc-1"

		class DummyAutomation:
			name = "dummy-auto-1"
			send_frequency = "Every time"
			phone_field = "phone"

		class DummyCtx:
			automation = DummyAutomation()
			doc = DummyDoc()
			value = None
			discriminator = None
			def get(self, key, default=None):
				if key == "phone":
					return "+919876543210"
				return default

		ctx = DummyCtx()
		step = DummyStep()

		with patch(f"{CH}.already_sent", return_value=False), \
		     patch(f"{BS}.is_opted_out", return_value=True) as mock_is_opted_out, \
		     patch(f"{CH}.send_whatsapp") as mock_send:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "skipped: opted out")
			mock_is_opted_out.assert_called_once_with("+919876543210")
			mock_send.assert_not_called()

	def test_marketing_template_not_opted_out_sends(self):
		"""Marketing WhatsApp template sent to an opted-in number proceeds to send."""
		tpl_name = self._make_template("test_promo_tpl_2", "MARKETING")

		class DummyStep:
			step_id = "s1"
			step_type = SEND_WHATSAPP
			template = tpl_name
			delivery_mode = "Direct"

		class DummyDoc:
			doctype = "ToDo"
			name = "dummy-doc-2"

		class DummyAutomation:
			name = "dummy-auto-2"
			send_frequency = "Every time"
			phone_field = "phone"

		class DummyCtx:
			automation = DummyAutomation()
			doc = DummyDoc()
			value = None
			discriminator = None
			def get(self, key, default=None):
				if key == "phone":
					return "+919876543210"
				return default

		ctx = DummyCtx()
		step = DummyStep()

		with patch(f"{CH}.already_sent", return_value=False), \
		     patch(f"{BS}.is_opted_out", return_value=False) as mock_is_opted_out, \
		     patch(f"{CH}.send_whatsapp", return_value=True) as mock_send:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "sent")
			mock_is_opted_out.assert_called_once_with("+919876543210")
			mock_send.assert_called_once()

	def test_utility_template_opted_out_sends(self):
		"""Utility template sent to an opted-out number is never blocked."""
		tpl_name = self._make_template("test_util_tpl", "UTILITY")

		class DummyStep:
			step_id = "s1"
			step_type = SEND_WHATSAPP
			template = tpl_name
			delivery_mode = "Direct"

		class DummyDoc:
			doctype = "ToDo"
			name = "dummy-doc-3"

		class DummyAutomation:
			name = "dummy-auto-3"
			send_frequency = "Every time"
			phone_field = "phone"

		class DummyCtx:
			automation = DummyAutomation()
			doc = DummyDoc()
			value = None
			discriminator = None
			def get(self, key, default=None):
				if key == "phone":
					return "+919876543210"
				return default

		ctx = DummyCtx()
		step = DummyStep()

		with patch(f"{CH}.already_sent", return_value=False), \
		     patch(f"{BS}.is_opted_out", return_value=True) as mock_is_opted_out, \
		     patch(f"{CH}.send_whatsapp", return_value=True) as mock_send:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "sent")
			# is_opted_out should not even be checked for utility
			mock_is_opted_out.assert_not_called()
			mock_send.assert_called_once()

	def test_bulk_campaign_marketing_opted_out_skips(self):
		"""Bulk campaign delivery mode also respects marketing opt-out."""
		tpl_name = self._make_template("test_promo_bulk", "MARKETING")

		class DummyStep:
			step_id = "s1"
			step_type = SEND_WHATSAPP
			template = tpl_name
			delivery_mode = "Bulk campaign"

		class DummyDoc:
			doctype = "ToDo"
			name = "dummy-doc-4"

		class DummyAutomation:
			name = "dummy-auto-4"
			send_frequency = "Every time"
			phone_field = "phone"

		class DummyCtx:
			automation = DummyAutomation()
			doc = DummyDoc()
			def get(self, key, default=None):
				return None

		ctx = DummyCtx()
		step = DummyStep()

		with patch(f"{CH}.already_sent", return_value=False), \
		     patch(f"{CH}.resolve_phone", return_value="+91 98765 43210"), \
		     patch(f"{BS}.is_opted_out", return_value=True) as mock_is_opted_out, \
		     patch(f"{CH}.dispatch_bulk") as mock_dispatch:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "skipped: opted out")
			mock_is_opted_out.assert_called_once_with("+919876543210")
			mock_dispatch.assert_not_called()

	def test_contact_only_opt_out_skips(self):
		"""STOP from a number outside every Audience flags only the Contact; marketing must still skip."""
		tpl_name = self._make_template("test_promo_tpl_contact_only", "MARKETING")
		step = frappe._dict(step_id="s1", step_type=SEND_WHATSAPP, template=tpl_name, delivery_mode="Direct")
		ctx = frappe._dict(
			automation=frappe._dict(name="dummy-auto-2", send_frequency="Every time", phone_field="phone"),
			doc=frappe._dict(doctype="ToDo", name="dummy-doc-2"), value=None, discriminator=None, phone="+919876543210",
		)

		def exists(doctype, filters=None):
			return doctype == "Contact" and filters.get("phone_e164") == "+919876543210" and filters.get("unsubscribed_whatsapp") == 1

		with patch(f"{CH}.already_sent", return_value=False), \
		     patch(f"{BS}.is_opted_out", return_value=False), \
		     patch("frappe.db.exists", side_effect=exists), \
		     patch(f"{CH}.send_whatsapp") as mock_send:
			self.assertEqual(steps.execute(ctx, step), "skipped: opted out")
			mock_send.assert_not_called()

	def test_h6_utility_whatsapp_to_opted_out_contact_sends(self):
		"""UTILITY WhatsApp to opted-out contact sends (never blocked by opt-out)."""
		tpl_name = self._make_template("test_util_optout_sends", "UTILITY")
		step = frappe._dict(step_id="s1", step_type=SEND_WHATSAPP, template=tpl_name, delivery_mode="Direct")
		ctx = frappe._dict(
			automation=frappe._dict(name="dummy-auto-util", send_frequency="Every time", phone_field="phone", whatsapp_account="W1"),
			doc=frappe._dict(doctype="ToDo", name="dummy-doc-util"),
			phone="+919876543210",
		)
		with patch(f"{CH}.already_sent", return_value=False), \
		     patch(f"{BS}.is_opted_out", return_value=True), \
		     patch(f"{CH}.send_to_phone", return_value={"ok": True}) as mock_send_to_phone:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "sent")
			mock_send_to_phone.assert_called_once()

	def test_h6_marketing_whatsapp_to_opted_out_contact_skipped(self):
		"""MARKETING WhatsApp to opted-out contact is skipped."""
		tpl_name = self._make_template("test_promo_optout_skips", "MARKETING")
		step = frappe._dict(step_id="s1", step_type=SEND_WHATSAPP, template=tpl_name, delivery_mode="Direct")
		ctx = frappe._dict(
			automation=frappe._dict(name="dummy-auto-mktg", send_frequency="Every time", phone_field="phone"),
			doc=frappe._dict(doctype="ToDo", name="dummy-doc-mktg"),
			phone="+919876543210",
		)
		with patch(f"{CH}.already_sent", return_value=False), \
		     patch(f"{BS}.is_opted_out", return_value=True), \
		     patch(f"{CH}.send_to_phone") as mock_send_to_phone:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "skipped: opted out")
			mock_send_to_phone.assert_not_called()

	def test_h6_sms_opted_out_contact_skipped(self):
		"""SMS automated send to opted-out contact is skipped via RecipientOptedOut."""
		step = frappe._dict(step_id="s1", step_type=SEND_SMS, sms_message="Test SMS")
		ctx = frappe._dict(
			automation=frappe._dict(name="dummy-auto-sms", phone_field="phone", sms_account="Test SMS"),
			doc=frappe._dict(doctype="ToDo", name="dummy-doc-sms", phone="+919876543210"),
		)
		with patch(f"{CH}.is_opted_out", return_value=True) as mock_opted_out, \
		     patch("msuite_sms.sms.providers.get_provider") as mock_get_provider:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "skipped: opted out")
			mock_opted_out.assert_called_once_with("SMS", "919876543210")
			mock_get_provider.assert_not_called()

	def test_h6_email_opted_out_contact_skipped(self):
		"""Email automated send to opted-out contact is skipped via RecipientOptedOut."""
		step = frappe._dict(step_id="s1", step_type=SEND_EMAIL, email_subject="Subject", email_message="Body")
		ctx = frappe._dict(
			automation=frappe._dict(name="dummy-auto-email", email_field="email"),
			doc=frappe._dict(doctype="ToDo", name="dummy-doc-email", email="optout@example.com"),
		)
		with patch(f"{CH}.is_opted_out", return_value=True) as mock_opted_out, \
		     patch("frappe.sendmail") as mock_sendmail:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "skipped: opted out")
			mock_opted_out.assert_called_once_with("Email", "optout@example.com")
			mock_sendmail.assert_not_called()

	def _exists_only(self, doctype, field):
		"""frappe.db.exists stub: true only for `doctype` rows filtered on `field` = 1."""
		def exists(dt, filters=None, *a, **kw):
			return bool(dt == doctype and isinstance(filters, dict) and filters.get(field) == 1)
		return exists

	def test_is_opted_out_sms_contact_only_flag(self):
		"""STOP from a number outside every Audience only flags the Contact."""
		with patch(f"{CH}.frappe.db.exists", side_effect=self._exists_only("Contact", "unsubscribed_sms")):
			self.assertTrue(channels.is_opted_out("SMS", "+919876543210"))

	def test_is_opted_out_email_contact_only_flag(self):
		with patch(f"{CH}.frappe.db.exists", side_effect=self._exists_only("Contact", "unsubscribed_email")), \
		     patch("msuite_email.msuite_email.doctype.msuite_email_unsubscribe.msuite_email_unsubscribe.is_suppressed", return_value=False):
			self.assertTrue(channels.is_opted_out("Email", "Optout@Example.com"))

	def test_is_opted_out_false_when_nothing_flagged(self):
		with patch(f"{CH}.frappe.db.exists", return_value=False), \
		     patch("msuite_email.msuite_email.doctype.msuite_email_unsubscribe.msuite_email_unsubscribe.is_suppressed", return_value=False):
			self.assertFalse(channels.is_opted_out("SMS", "+919876543210"))
			self.assertFalse(channels.is_opted_out("Email", "ok@example.com"))
