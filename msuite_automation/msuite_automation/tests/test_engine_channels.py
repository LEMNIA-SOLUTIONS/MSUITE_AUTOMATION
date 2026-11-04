import unittest
from unittest.mock import MagicMock, patch

import frappe

from msuite_automation.msuite_automation.engine import channels, filters

CH = "msuite_automation.msuite_automation.engine.channels"


class TestFilters(unittest.TestCase):
	def test_rule_ok(self):
		self.assertTrue(filters.rule_ok("600", ">", "500"))
		self.assertTrue(filters.rule_ok("Paid", "in", "paid, cod"))
		self.assertTrue(filters.rule_ok("", "is not set", None))
		self.assertFalse(filters.rule_ok("x", "contains", ""))

	def test_rules_pass_logic_and_awb_alias(self):
		doc = frappe._dict(doctype="Sales Order", grand_total=100, lr_no="LR1")
		rules = [{"field": "grand_total", "operator": ">", "value": "500"}, {"field": "awb_number", "operator": "is set"}]
		self.assertFalse(filters.rules_pass(rules, "All", doc))
		self.assertTrue(filters.rules_pass(rules, "Any", doc))
		self.assertTrue(filters.rules_pass([], "All", doc))


class TestChannels(unittest.TestCase):
	def test_resolve_phone_field_then_fallback(self):
		a = frappe._dict(phone_field="custom_phone")
		self.assertEqual(channels.resolve_phone(a, frappe._dict(custom_phone="", mobile_no="+91 98765 43210")), "919876543210")

	@patch(f"{CH}.send_to_phone", return_value={"ok": True})
	@patch(f"{CH}._template")
	def test_send_whatsapp_builds_components(self, tpl, send):
		tpl.return_value = frappe._dict(name="t", language_code="en", parameter_format="POSITIONAL", template="Hi {{1}}",
			header_type=None, buttons=[])
		a = frappe._dict(name="MA-1", whatsapp_account="acc", phone_field=None,
			variable_mappings=[frappe._dict(step_id="wa", variable_key="1", source_field="first_name", static_fallback="there")])
		step = frappe._dict(step_id="wa", template="t", header_source="None", button_fields=None)
		doc = frappe._dict(doctype="CRM Lead", name="L1", first_name="", mobile_no="9876543210")
		self.assertTrue(channels.send_whatsapp(a, step, doc))
		payload = send.call_args.kwargs["payload"]
		self.assertEqual(payload["components"][0]["parameters"][0]["text"], "there")
		self.assertEqual(send.call_args.kwargs["whatsapp_account"], "acc")
		self.assertEqual(send.call_args.kwargs["reference_name"], "L1")

	@patch(f"{CH}.frappe.sendmail")
	def test_send_email(self, sendmail):
		a = frappe._dict(name="MA-1", email_field="email_id")
		step = frappe._dict(step_id="e", email_subject="Hi {{ doc.name }}", email_body="B", attach_print=0)
		self.assertTrue(channels.send_email(a, step, frappe._dict(doctype="ToDo", name="T1", email_id="a@b.c")))
		self.assertEqual(sendmail.call_args.kwargs["subject"], "Hi T1")

	def test_email_missing_recipient(self):
		a = frappe._dict(name="MA-1", email_field=None)
		self.assertFalse(channels.send_email(a, frappe._dict(step_id="e"), frappe._dict(doctype="ToDo", name="T1")))

	@patch(f"{CH}.frappe.db.exists", return_value=True)
	def test_already_sent(self, exists):
		self.assertTrue(channels.already_sent("t", "CRM Lead", "L1", "Once per document"))
		self.assertFalse(channels.already_sent("t", "CRM Lead", "L1", "Every time"))
		filters_used = exists.call_args.args[1]
		self.assertEqual(filters_used["template_name"], "t")

	@patch(f"{CH}.frappe.db.commit")
	@patch(f"{CH}.frappe.get_doc")
	def test_add_to_list_does_not_commit(self, get_doc, mock_commit):
		mock_list = MagicMock()
		get_doc.return_value = mock_list
		res = channels.add_to_list("RL-1", "919999999999", frappe._dict(doctype="ToDo", name="T1"))
		self.assertTrue(res)
		mock_commit.assert_not_called()

	@patch(f"{CH}.already_sent", return_value=True)
	def test_bulk_campaign_checks_already_sent(self, mock_already_sent):
		from msuite_automation.msuite_automation.engine import steps as S
		a = frappe._dict(name="MA-1", send_frequency="Once per document")
		doc = frappe._dict(doctype="CRM Lead", name="L1", mobile_no="919999999999")
		step = frappe._dict(step_id="wa", step_type="Send WhatsApp", delivery_mode="Bulk campaign", template="tpl")
		ctx = frappe._dict(automation=a, doc=doc)
		result = S.execute(ctx, step)
		self.assertEqual(result, "skipped: already sent")
		mock_already_sent.assert_called_once_with("tpl", "CRM Lead", "L1", "Once per document", ignore_failed=False)

	@patch(f"{CH}.send_to_phone")
	@patch(f"{CH}._template")
	def test_build_whatsapp_does_not_send(self, tpl, send):
		tpl.return_value = frappe._dict(name="t", language_code="en", parameter_format="POSITIONAL", template="Hi {{1}}",
			header_type=None, buttons=[])
		a = frappe._dict(name="MA-1", whatsapp_account="acc", phone_field=None,
			variable_mappings=[frappe._dict(step_id="wa", variable_key="1", source_field="first_name", static_fallback="there")])
		step = frappe._dict(step_id="wa", template="t", header_source="None", button_fields=None)
		doc = frappe._dict(doctype="CRM Lead", name="L1", first_name="Asha", mobile_no="9876543210")
		msg = channels.build_whatsapp(a, step, doc)
		send.assert_not_called()
		self.assertEqual(msg["template"], "t")
		self.assertEqual(msg["account"], "acc")
		self.assertEqual(msg["payload"]["components"][0]["parameters"][0]["text"], "Asha")
		self.assertTrue(msg["to"])

	def test_build_whatsapp_no_phone(self):
		a = frappe._dict(name="MA-1", phone_field=None, variable_mappings=[])
		self.assertIsNone(channels.build_whatsapp(a, frappe._dict(step_id="wa", template="t"), frappe._dict(doctype="ToDo", name="X")))

	def test_render_email_and_sms(self):
		a = frappe._dict(name="MA-1", email_field="email_id", phone_field="mobile_no")
		doc = frappe._dict(doctype="ToDo", name="X", description="Order 42", email_id="a@example.com", mobile_no="9876543210")
		email = channels.render_email(a, frappe._dict(email_subject="About {{ doc.description }}", email_body="Body {{ doc.name }}"), doc)
		self.assertEqual((email["to"], email["subject"], email["message"]), ("a@example.com", "About Order 42", "Body X"))
		sms = channels.render_sms(a, frappe._dict(sms_message="Hi {{ doc.description }}"), doc)
		self.assertEqual(sms["message"], "Hi Order 42")
		self.assertTrue(sms["to"])
		self.assertIsNone(channels.render_email(frappe._dict(name="MA-1"), frappe._dict(), frappe._dict(doctype="ToDo", name="X")))

	@patch(f"{CH}.send_to_phone", return_value={"ok": True})
	@patch(f"{CH}._template")
	def test_send_whatsapp_carries_chosen_number(self, tpl, send):
		tpl.return_value = frappe._dict(name="t", language_code="en", parameter_format="POSITIONAL", template="Hi", header_type=None, buttons=[])
		step = frappe._dict(step_id="wa", template="t", header_source="None", button_fields=None)
		doc = frappe._dict(doctype="CRM Lead", name="L1", mobile_no="9876543210")
		a = frappe._dict(name="MA-1", whatsapp_account="acc", whatsapp_phone="P2", phone_field=None, variable_mappings=[])
		channels.send_whatsapp(a, step, doc)
		self.assertEqual(send.call_args.kwargs["payload"]["phone_number_id"], "P2")
		a.whatsapp_phone = ""
		channels.send_whatsapp(a, step, doc)
		self.assertNotIn("phone_number_id", send.call_args.kwargs["payload"])


class TestAcquireLock(unittest.TestCase):
	def _cache(self, result=None, error=None):
		cache = MagicMock()
		cache.make_key.side_effect = lambda k: f"site|{k}"
		if error:
			cache.set.side_effect = error
		else:
			cache.set.return_value = result
		return cache

	def test_acquired(self):
		cache = self._cache(result=True)
		with patch(f"{CH}.frappe.cache", return_value=cache):
			self.assertTrue(channels.acquire_lock("k", ttl=60))
		cache.set.assert_called_once_with("site|k", "1", nx=True, ex=60)

	def test_already_held(self):
		with patch(f"{CH}.frappe.cache", return_value=self._cache(result=None)):
			self.assertFalse(channels.acquire_lock("k"))

	def test_redis_error_raises_lock_unavailable(self):
		with patch(f"{CH}.frappe.cache", return_value=self._cache(error=ConnectionError("down"))):
			with self.assertRaises(channels.LockUnavailable):
				channels.acquire_lock("k")

	def test_release_lock_logs_warning_on_error(self):
		cache = MagicMock()
		cache.make_key.side_effect = lambda k: f"site|{k}"
		cache.delete.side_effect = ConnectionError("down")
		with patch(f"{CH}.frappe.cache", return_value=cache), patch(f"{CH}.frappe.logger") as mock_logger:
			channels.release_lock("k")
			mock_logger.assert_called_with(channels.LOGGER_NAME)
			mock_logger.return_value.warning.assert_called_once()
			self.assertIn("release_lock failed for k", mock_logger.return_value.warning.call_args[0][0])

	def test_release_lock_success(self):
		cache = MagicMock()
		cache.make_key.side_effect = lambda k: f"site|{k}"
		with patch(f"{CH}.frappe.cache", return_value=cache):
			channels.release_lock("k")
			cache.delete.assert_called_once_with("site|k")
