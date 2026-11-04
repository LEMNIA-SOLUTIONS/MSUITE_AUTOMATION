import unittest
from unittest.mock import MagicMock, patch

import frappe
from frappe.tests import IntegrationTestCase

from msuite_automation.msuite_automation.engine import channels
from msuite_whatsapp.services.bulk import bulk_send_service as bulk
from msuite_whatsapp.services.bulk import rate_limit_service as rl

CH = "msuite_automation.msuite_automation.engine.channels"
RL = "msuite_whatsapp.services.bulk.rate_limit_service"
MLS = "msuite_whatsapp.services.accounts.messaging_limit_service"
REPO = "msuite_whatsapp.queries.account_repository"


class TestAutomationRateLimit(IntegrationTestCase):
	def setUp(self):
		super().setUp()
		rl.reset_account_memo()

	def tearDown(self):
		rl.reset_account_memo()
		super().tearDown()

	def _dummy_template(self):
		return frappe._dict(
			name="tpl_hello",
			language_code="en",
			parameter_format="POSITIONAL",
			template="Hello",
			header_type=None,
			buttons=[],
		)

	@patch(f"{CH}.send_to_phone")
	@patch(f"{CH}._template")
	@patch(f"{RL}.check_and_claim", side_effect=rl.RateLimitExceeded("limit reached"))
	@patch(f"{MLS}.get_portfolio_tier", return_value="TIER_50")
	def test_send_whatsapp_over_cap_raises_send_retry(self, mock_tier, mock_claim, mock_tpl, mock_send):
		mock_tpl.return_value = self._dummy_template()
		a = frappe._dict(name="MA-1", whatsapp_account="acc1", phone_field=None, variable_mappings=[])
		step = frappe._dict(step_id="w1", template="tpl_hello", header_source="None", button_fields=None)
		doc = frappe._dict(doctype="CRM Lead", name="L1", mobile_no="919876543210")

		mock_account = frappe._dict(name="acc1", business_id="biz1", phones=[])
		with patch(f"{REPO}.get_account_doc", return_value=mock_account):
			with self.assertRaises(channels.SendRetry) as ctx:
				channels.send_whatsapp(a, step, doc)

		self.assertEqual(ctx.exception.kind, "limit")
		self.assertIn("WhatsApp portfolio messaging limit reached", str(ctx.exception))
		mock_send.assert_not_called()

	@patch(f"{CH}.send_to_phone", return_value={"ok": True})
	@patch(f"{CH}._template")
	@patch(f"{RL}.check_and_claim")
	@patch(f"{MLS}.get_portfolio_tier", return_value="TIER_50")
	def test_send_whatsapp_under_cap_sends(self, mock_tier, mock_claim, mock_tpl, mock_send):
		mock_tpl.return_value = self._dummy_template()
		a = frappe._dict(name="MA-1", whatsapp_account="acc1", phone_field=None, variable_mappings=[])
		step = frappe._dict(step_id="w1", template="tpl_hello", header_source="None", button_fields=None)
		doc = frappe._dict(doctype="CRM Lead", name="L1", mobile_no="919876543210")

		mock_account = frappe._dict(name="acc1", business_id="biz1", phones=[])
		with patch(f"{REPO}.get_account_doc", return_value=mock_account):
			res = channels.send_whatsapp(a, step, doc)

		self.assertTrue(res)
		mock_send.assert_called_once()
		mock_claim.assert_called_once()

	def test_claim_for_account_same_recipient_twice_second_is_free(self):
		mock_account = frappe._dict(name="acc1", business_id="biz1", phones=[])
		known = set()

		def fake_eval(script, numkeys, key, now, window, limit, recipient):
			if recipient not in known and len(known) >= limit:
				return 0
			known.add(recipient)
			return 1

		mock_cache = MagicMock()
		mock_cache.make_key.side_effect = lambda k: f"test:{k}"
		mock_cache.eval.side_effect = fake_eval

		with patch(f"{REPO}.get_account_doc", return_value=mock_account), \
			patch(f"{MLS}.get_portfolio_tier", return_value="TIER_50"), \
			patch.object(frappe, "cache", return_value=mock_cache):

			# First send claims a slot
			self.assertTrue(rl.claim_for_account("acc1", "919876543210"))
			self.assertIn("919876543210", known)

			# Fill up remaining 49 slots to reach 50
			for i in range(49):
				known.add(f"other_{i}")

			# New recipient is blocked because cap of 50 is reached
			self.assertFalse(rl.claim_for_account("acc1", "919999999999"))

			# Same recipient messaged earlier is free
			self.assertTrue(rl.claim_for_account("acc1", "919876543210"))

	@patch("msuite_inbox.inbox.services.outbound._default_inbox_for_channel", return_value="INBOX-1")
	@patch(f"{RL}.check_and_claim")
	@patch(f"{MLS}.get_portfolio_tier", return_value="TIER_50")
	def test_claim_for_account_none_resolves_via_default_inbox(self, mock_tier, mock_claim, mock_default_inbox):
		mock_account = frappe._dict(name="inbox_wa_acc", business_id="biz_inbox", phones=[])
		with patch.object(frappe.db, "get_value", return_value="inbox_wa_acc") as mock_db_get, \
			patch(f"{REPO}.get_account_doc", return_value=mock_account) as mock_get_account:

			res = rl.claim_for_account(None, "919876543210")
			self.assertTrue(res)
			mock_default_inbox.assert_called_once_with("WhatsApp")
			mock_db_get.assert_called_once_with("Inbox", "INBOX-1", "whatsapp_account")
			mock_get_account.assert_any_call("inbox_wa_acc")
			mock_claim.assert_called_once_with("biz_inbox", "919876543210", "TIER_50")

	@patch(f"{RL}.check_and_claim")
	@patch(f"{MLS}.get_portfolio_tier", return_value="TIER_50")
	def test_claim_for_account_memoizes_account_and_tier(self, mock_tier, mock_claim):
		mock_account = frappe._dict(name="acc1", business_id="biz1", phones=[])
		with patch(f"{REPO}.get_account_doc", return_value=mock_account) as mock_get_account:
			res1 = rl.claim_for_account("acc1", "919876543210")
			# First claim fetches account (in claim_for_account and get_default_phone) and tier
			self.assertEqual(mock_get_account.call_count, 2)
			self.assertEqual(mock_tier.call_count, 1)

			res2 = rl.claim_for_account("acc1", "919876543211")
			self.assertTrue(res1)
			self.assertTrue(res2)
			# Second claim hits the memo: no additional get_account_doc or get_portfolio_tier calls
			self.assertEqual(mock_get_account.call_count, 2)
			self.assertEqual(mock_tier.call_count, 1)
			self.assertEqual(mock_claim.call_count, 2)

	@patch(f"{RL}.check_and_claim")
	@patch(f"{MLS}.get_portfolio_tier", return_value="TIER_50")
	def test_claim_for_account_none_no_inbox_resolves_via_default_account(self, mock_tier, mock_claim):
		mock_account = frappe._dict(name="default_wa_acc", business_id="biz_default", phones=[])
		orig_get_value = frappe.db.get_value

		def fake_get_value_no_inbox(doctype, *args, **kwargs):
			if doctype == "Inbox":
				return None
			return orig_get_value(doctype, *args, **kwargs)

		with patch.object(frappe.db, "get_value", side_effect=fake_get_value_no_inbox), \
			patch(f"{REPO}.get_account_doc", return_value=mock_account) as mock_get_account:

			res = rl.claim_for_account(None, "919876543210")
			self.assertTrue(res)
			mock_get_account.assert_any_call(None)
			mock_claim.assert_called_once_with("biz_default", "919876543210", "TIER_50")

	@patch(f"{MLS}.get_portfolio_tier", return_value="TIER_50")
	def test_claim_for_account_failure_logs_warning_and_fails_open(self, mock_tier):
		def raise_account_error(*args, **kwargs):
			frappe.throw("No active WhatsApp Account found.")

		with patch("frappe.logger") as mock_logger, \
			patch(f"{REPO}.get_account_doc", side_effect=raise_account_error):

			res = rl.claim_for_account("missing_acc", "919876543210")
			self.assertTrue(res)
			mock_logger.return_value.warning.assert_called_once_with(
				"WhatsApp portfolio claim failed for account %s (%s) - failing open",
				"missing_acc",
				"ValidationError",
			)

	@patch(f"{CH}.send_to_phone", return_value={"ok": True})
	@patch(f"{CH}._template")
	@patch(f"{MLS}.get_portfolio_tier", return_value="TIER_50")
	def test_no_default_account_or_inbox_fails_open_and_sends(self, mock_tier, mock_tpl, mock_send):
		mock_tpl.return_value = self._dummy_template()
		a = frappe._dict(name="MA-1", whatsapp_account=None, phone_field=None, variable_mappings=[])
		step = frappe._dict(step_id="w1", template="tpl_hello", header_source="None", button_fields=None)
		doc = frappe._dict(doctype="CRM Lead", name="L1", mobile_no="919876543210")
		orig_get_value = frappe.db.get_value

		def fake_get_value_no_inbox(doctype, *args, **kwargs):
			if doctype == "Inbox":
				return None
			return orig_get_value(doctype, *args, **kwargs)

		def raise_not_found(*args, **kwargs):
			frappe.throw("No active WhatsApp Account found.")

		# Case 1: no inbox found and no default account -> fails open
		with patch.object(frappe.db, "get_value", side_effect=fake_get_value_no_inbox), \
			patch(f"{REPO}.get_account_doc", side_effect=raise_not_found):
			self.assertTrue(rl.claim_for_account(None, "919876543210"))
			res = channels.send_whatsapp(a, step, doc)
			self.assertTrue(res)
			mock_send.assert_called_once()

		mock_send.reset_mock()

		# Case 2: account throws (e.g. no default account configured) -> fails open
		with patch(f"{REPO}.get_account_doc", side_effect=raise_not_found):
			self.assertTrue(rl.claim_for_account("missing_acc", "919876543210"))
			a.whatsapp_account = "missing_acc"
			res = channels.send_whatsapp(a, step, doc)
			self.assertTrue(res)
			mock_send.assert_called_once()

	def test_bulk_is_rate_limited_delegates_to_claim_for_account(self):
		with patch("msuite_whatsapp.services.bulk.bulk_send_service.claim_for_account", return_value=False) as mock_claim:
			self.assertTrue(bulk._is_rate_limited(frappe._dict(whatsapp_account="acc1"), "919876543210"))
			mock_claim.assert_called_once_with("acc1", "919876543210", fail_open=False)

		with patch("msuite_whatsapp.services.bulk.bulk_send_service.claim_for_account", return_value=True) as mock_claim:
			self.assertFalse(bulk._is_rate_limited(frappe._dict(whatsapp_account="acc1"), "919876543210"))
			mock_claim.assert_called_once_with("acc1", "919876543210", fail_open=False)

		# Missing account or phone is never rate limited
		self.assertFalse(bulk._is_rate_limited(frappe._dict(whatsapp_account=None), "919876543210"))
		self.assertFalse(bulk._is_rate_limited(frappe._dict(whatsapp_account="acc1"), ""))

	def test_claim_for_account_fail_open_false_reraises_on_account_error(self):
		with patch(f"{REPO}.get_account_doc", side_effect=frappe.ValidationError("Account lookup failed")):
			with self.assertRaises(frappe.ValidationError):
				rl.claim_for_account("bad_account", "919876543210", fail_open=False)

	def test_claim_for_account_default_returns_true_when_it_raises(self):
		with patch(f"{REPO}.get_account_doc", side_effect=frappe.ValidationError("Account lookup failed")):
			self.assertTrue(rl.claim_for_account("bad_account", "919876543210"))

	def test_claim_for_account_fail_open_false_returns_false_on_rate_limit_exceeded(self):
		mock_account = frappe._dict(name="acc1", business_id="biz1", phones=[])
		with patch(f"{REPO}.get_account_doc", return_value=mock_account), \
			patch(f"{MLS}.get_portfolio_tier", return_value="TIER_50"), \
			patch(f"{RL}.check_and_claim", side_effect=rl.RateLimitExceeded("limit reached")):
			self.assertFalse(rl.claim_for_account("acc1", "919876543210", fail_open=False))

	def test_bulk_is_rate_limited_passes_fail_open_false(self):
		with patch("msuite_whatsapp.services.bulk.bulk_send_service.claim_for_account", return_value=True) as mock_claim:
			self.assertFalse(bulk._is_rate_limited(frappe._dict(whatsapp_account="acc1"), "919876543210"))
			mock_claim.assert_called_once_with("acc1", "919876543210", fail_open=False)

