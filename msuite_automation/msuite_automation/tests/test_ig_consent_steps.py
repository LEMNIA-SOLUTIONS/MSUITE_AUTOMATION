# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Tests for Instagram mention consent automation steps."""

from __future__ import annotations

import json
import unittest
from unittest.mock import MagicMock, patch

import frappe
import requests

from msuite_core.constants import (
	IG_CONSENT_DM_RECEIVED,
	IG_CONSENT_DOCTYPE,
	IG_CONSENT_REPLY_FAILED,
	IG_CONSENT_REPLY_SENT,
	IG_CONSENT_REPLY_SKIPPED,
	MESSENGER_BUTTON_TITLE_MAX,
	MESSENGER_TEMPLATE_SUBTITLE_MAX,
	MESSENGER_TEMPLATE_TITLE_MAX,
	SOCIAL_MENTION_DOCTYPE,
)
from msuite_automation.msuite_automation.constants import (
	IG_CONSENT_DEFAULT_EXPIRY_DAYS,
	IG_CONSENT_NOTE_IG_LOGIN,
)
from msuite_inbox.inbox.constants import IG_CONSENT_FAILED, IG_CONSENT_PENDING_DM
from msuite_inbox.inbox.services.mentions.graph import MentionTransient, MentionUnresolvable
from msuite_automation.msuite_automation import constants as C
from msuite_automation.msuite_automation.constants import (
	ACTION_TYPES,
	SEND_IG_CONSENT_TEMPLATE,
	SEND_IG_PUBLIC_REPLY,
	SEND_STEPS,
	STEP_TYPES,
	WAIT_EVENT,
)
from msuite_automation.msuite_automation.engine import channels, preview, steps
from msuite_automation.msuite_automation.engine.graph import validate_graph, validate_warnings


class TestIGConsentConstants(unittest.TestCase):
	"""Test that constants are correctly registered."""

	def test_constants_membership(self):
		self.assertIn(SEND_IG_PUBLIC_REPLY, STEP_TYPES)
		self.assertIn(SEND_IG_PUBLIC_REPLY, ACTION_TYPES)
		self.assertNotIn(SEND_IG_PUBLIC_REPLY, SEND_STEPS)

		self.assertIn(SEND_IG_CONSENT_TEMPLATE, STEP_TYPES)
		self.assertIn(SEND_IG_CONSENT_TEMPLATE, ACTION_TYPES)
		self.assertNotIn(SEND_IG_CONSENT_TEMPLATE, SEND_STEPS)


class TestIGConsentSteps(unittest.TestCase):
	"""Unit and integration tests for IG public reply and consent template steps."""

	def setUp(self):
		self._created_docs = []
		self._cleanup()
		self.account_name = self._make_account()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		frappe.cache().delete_keys("ig_mention_reply:")
		frappe.cache().delete_keys("ig_consent_card:")
		if hasattr(self, "_created_docs") and self._created_docs:
			mention_names = [name for dt, name in self._created_docs if dt == SOCIAL_MENTION_DOCTYPE]
			if mention_names:
				for name in frappe.get_all(IG_CONSENT_DOCTYPE, filters={"social_mention": ("in", mention_names)}, pluck="name"):
					frappe.delete_doc(IG_CONSENT_DOCTYPE, name, force=True, ignore_permissions=True)
			for dt, name in reversed(self._created_docs):
				if frappe.db.exists(dt, name):
					frappe.delete_doc(dt, name, force=True, ignore_permissions=True)
			self._created_docs.clear()

	def _make_account(self, account_id="test_ig_acc_1"):
		doc = frappe.get_doc({
			"doctype": "Social Account",
			"platform": "Instagram",
			"account_id": account_id,
			"account_name": f"Account {account_id}",
			"status": "Active",
		})
		doc.flags.ignore_permissions = doc.flags.ignore_mandatory = doc.flags.ignore_links = True
		doc.insert()
		if hasattr(self, "_created_docs"):
			self._created_docs.append((doc.doctype, doc.name))
		return doc.name

	def _make_mention(
		self,
		interaction_type="comment_mention",
		comment_id="comm_1",
		post_id="post_1",
		username="alice",
		social_account=None,
	):
		doc = frappe.get_doc({
			"doctype": SOCIAL_MENTION_DOCTYPE,
			"platform": "Instagram",
			"social_account": social_account or self.account_name,
			"interaction_type": interaction_type,
			"comment_id": comment_id,
			"post_id": post_id,
			"dedupe_key": f"ig:mention:{post_id}:{comment_id or 'cap'}:{username}",
			"username": username,
			"permalink": f"https://instagram.com/p/{post_id}",
			"media_urls": json.dumps(["https://cdn.example.com/p.jpg"]),
			"text": f"@{username} hello",
		})
		doc.flags.ignore_permissions = doc.flags.ignore_mandatory = doc.flags.ignore_links = True
		doc.insert()
		if hasattr(self, "_created_docs"):
			self._created_docs.append((doc.doctype, doc.name))
		return doc

	def _make_step(self, step_type, **fields):
		return frappe._dict(step_id="s1", step_type=step_type, **fields)

	def _make_ctx(self, doc, run="RUN-1", automation_name="AUTO-1"):
		automation = frappe._dict(name=automation_name, send_frequency="Every time")
		return frappe._dict(doc=doc, automation=automation, run=run)

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", return_value="reply_123")
	@patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_comment_mention(self, mock_ig_login, mock_token, mock_reply):
		doc = self._make_mention(interaction_type="comment_mention", comment_id="c_101", post_id="p_101", username="bob")
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks @{{ username }}! Reply YES")
		ctx = self._make_ctx(doc)

		res = steps.execute(ctx, step)
		self.assertEqual(res, "sent")

		mock_reply.assert_called_once_with(
			"test_ig_acc_1",
			"fake_token",
			message="Thanks @bob! Reply YES",
			media_id="p_101",
			comment_id="c_101",
		)

		consent_name = frappe.db.get_value(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}, "name")
		self.assertTrue(consent_name)
		consent = frappe.get_doc(IG_CONSENT_DOCTYPE, consent_name)
		self.assertEqual(consent.status, IG_CONSENT_PENDING_DM)
		self.assertEqual(consent.reply_status, IG_CONSENT_REPLY_SENT)
		self.assertEqual(consent.public_reply_id, "reply_123")

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", return_value="reply_456")
	@patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_caption_mention(self, mock_ig_login, mock_token, mock_reply):
		doc = self._make_mention(interaction_type="caption_mention", comment_id=None, post_id="p_202", username="charlie")
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Love your post @{{ username }}!")
		ctx = self._make_ctx(doc)

		res = steps.execute(ctx, step)
		self.assertEqual(res, "sent")

		mock_reply.assert_called_once_with(
			"test_ig_acc_1",
			"fake_token",
			message="Love your post @charlie!",
			media_id="p_202",
			comment_id=None,
		)

		consent_name = frappe.db.get_value(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}, "name")
		self.assertTrue(consent_name)
		consent = frappe.get_doc(IG_CONSENT_DOCTYPE, consent_name)
		self.assertEqual(consent.reply_status, IG_CONSENT_REPLY_SENT)

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply")
	def test_public_reply_unsupported_types(self, mock_reply):
		for itype in ("story_mention", "media_tag", "post_tag"):
			with self.subTest(itype=itype):
				doc = self._make_mention(interaction_type=itype, comment_id=None, post_id=f"p_unsupported_{itype}")
				step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks!")
				ctx = self._make_ctx(doc)

				res = steps.execute(ctx, step)
				self.assertEqual(res, "skipped: not a supported mention type")
				mock_reply.assert_not_called()
				self.assertFalse(frappe.db.exists(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}))

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=True)
	def test_public_reply_instagram_login_account(self, mock_ig_login, mock_reply):
		doc = self._make_mention()
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks!")
		ctx = self._make_ctx(doc)

		res = steps.execute(ctx, step)
		self.assertEqual(res, "skipped: instagram login account")
		mock_reply.assert_not_called()

		consent_name = frappe.db.get_value(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}, "name")
		self.assertTrue(consent_name)
		consent = frappe.get_doc(IG_CONSENT_DOCTYPE, consent_name)
		self.assertEqual(consent.status, IG_CONSENT_PENDING_DM)
		self.assertEqual(consent.reply_status, IG_CONSENT_REPLY_SKIPPED)
		self.assertEqual(consent.reply_note, IG_CONSENT_NOTE_IG_LOGIN)

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", return_value="reply_789")
	@patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_duplicate_rerun(self, mock_ig_login, mock_token, mock_reply):
		doc = self._make_mention()
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks @{{ username }}")
		ctx = self._make_ctx(doc)

		first = steps.execute(ctx, step)
		self.assertEqual(first, "sent")
		self.assertEqual(mock_reply.call_count, 1)

		second = steps.execute(ctx, step)
		self.assertEqual(second, "skipped: duplicate")
		self.assertEqual(mock_reply.call_count, 1)
		self.assertEqual(frappe.db.count(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}), 1)

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", side_effect=MentionTransient("temporary 5xx"))
	@patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_transient_error_releases_lock(self, mock_ig_login, mock_token, mock_reply):
		doc = self._make_mention()
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks @{{ username }}")
		ctx = self._make_ctx(doc)

		with patch("msuite_automation.msuite_automation.engine.channels.release_lock") as mock_release:
			with self.assertRaises(channels.SendRetry) as cm:
				steps.execute(ctx, step)
			self.assertEqual(cm.exception.kind, "transient")
			mock_release.assert_called_once_with(f"ig_mention_reply:{doc.name}")

	@patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_transient_then_rerun_posts(self, mock_ig_login, mock_token):
		"""A transient failure releases the lock so the retry posts and creates the record."""
		doc = self._make_mention()
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks @{{ username }}")
		ctx = self._make_ctx(doc)
		with patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", side_effect=MentionTransient("5xx")):
			with self.assertRaises(channels.SendRetry):
				steps.execute(ctx, step)
		self.assertFalse(frappe.db.exists(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}))
		with patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", return_value="r1") as mock_reply:
			self.assertEqual(steps.execute(ctx, step), "sent")
		mock_reply.assert_called_once()

	@patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_user_not_mentioned_refusal_records_failed_reply(self, mock_ig_login, mock_token):
		"""Meta's code-200/2207014 refusal flows through graph into a Failed-reply record, no account flag."""
		from msuite_inbox.inbox.constants import IG_ERR_SUBCODE_USER_NOT_MENTIONED

		user_msg = "The user should be mentioned in either the media caption or the specific comment."
		resp = MagicMock(ok=False, status_code=400)
		resp.json.return_value = {"error": {"code": 200, "error_subcode": IG_ERR_SUBCODE_USER_NOT_MENTIONED, "message": "Permissions error", "error_user_msg": user_msg}}
		doc = self._make_mention(interaction_type="comment_mention")
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks")
		with patch("msuite_inbox.inbox.services.mentions.graph.requests.post", return_value=resp), patch(
			"msuite_automation.msuite_automation.engine.steps.flag_if_auth_failure"
		) as mock_flag:
			res = steps.execute(self._make_ctx(doc), step)
		self.assertEqual(res, "skipped: reply refused")
		mock_flag.assert_not_called()
		consent = frappe.get_doc(IG_CONSENT_DOCTYPE, {"social_mention": doc.name})
		self.assertEqual(consent.reply_status, IG_CONSENT_REPLY_FAILED)
		self.assertEqual(consent.reply_note, user_msg)

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", side_effect=MentionUnresolvable("Meta refused reply"))
	@patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_unresolvable_error(self, mock_ig_login, mock_token, mock_reply):
		doc = self._make_mention()
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks @{{ username }}")
		ctx = self._make_ctx(doc)

		res = steps.execute(ctx, step)
		self.assertEqual(res, "skipped: reply refused")

		consent_name = frappe.db.get_value(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}, "name")
		self.assertTrue(consent_name)
		consent = frappe.get_doc(IG_CONSENT_DOCTYPE, consent_name)
		self.assertEqual(consent.status, IG_CONSENT_PENDING_DM)
		self.assertEqual(consent.reply_status, IG_CONSENT_REPLY_FAILED)
		self.assertIn("Meta refused reply", consent.reply_note)

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", side_effect=requests.HTTPError("401 Unauthorized"))
	@patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	@patch("msuite_automation.msuite_automation.engine.steps.flag_if_auth_failure")
	def test_public_reply_http_error(self, mock_flag, mock_ig_login, mock_token, mock_reply):
		doc = self._make_mention()
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks @{{ username }}")
		ctx = self._make_ctx(doc)

		with self.assertRaises(requests.HTTPError):
			steps.execute(ctx, step)

		mock_flag.assert_called_once()
		self.assertEqual(mock_flag.call_args[0][0], self.account_name)
		self.assertFalse(frappe.db.exists(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}))
		self.assertIsNone(frappe.cache().get(frappe.cache().make_key(f"ig_mention_reply:{doc.name}")))

		# Rerun once token/account is fixed posts successfully
		with patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", return_value="reply_fixed") as mock_fixed:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "sent")
			mock_fixed.assert_called_once()
		self.assertTrue(frappe.db.exists(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}))

	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_account_load_error_releases_lock(self, mock_ig_login):
		"""Account load error propagates and releases dedupe lock so rerun can post."""
		doc = self._make_mention(social_account="Nonexistent_Account_XYZ")
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks @{{ username }}")
		ctx = self._make_ctx(doc)

		with self.assertRaises(frappe.DoesNotExistError):
			steps.execute(ctx, step)

		self.assertFalse(frappe.db.exists(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}))
		self.assertIsNone(frappe.cache().get(frappe.cache().make_key(f"ig_mention_reply:{doc.name}")))

		# Rerun once account is valid posts successfully
		doc.social_account = self.account_name
		with patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token"), \
			patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", return_value="reply_ok") as mock_reply:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "sent")
			mock_reply.assert_called_once()

	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_token_load_error_releases_lock(self, mock_ig_login):
		"""Token load failure propagates and releases dedupe lock so rerun can post."""
		doc = self._make_mention()
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks @{{ username }}")
		ctx = self._make_ctx(doc)

		with patch("msuite_core.services.connections.tokens.get_valid_token", side_effect=RuntimeError("Token provider down")):
			with self.assertRaises(RuntimeError):
				steps.execute(ctx, step)

		self.assertFalse(frappe.db.exists(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}))
		self.assertIsNone(frappe.cache().get(frappe.cache().make_key(f"ig_mention_reply:{doc.name}")))

		# Rerun once token provider recovers
		with patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token"), \
			patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", return_value="reply_ok") as mock_reply:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "sent")
			mock_reply.assert_called_once()

	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_missing_token_releases_lock(self, mock_ig_login):
		"""Empty or missing token releases dedupe lock so rerun can post once token exists."""
		doc = self._make_mention()
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks @{{ username }}")
		ctx = self._make_ctx(doc)

		with patch("msuite_core.services.connections.tokens.get_valid_token", return_value=""):
			with self.assertRaises(ValueError):
				steps.execute(ctx, step)

		self.assertFalse(frappe.db.exists(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}))
		self.assertIsNone(frappe.cache().get(frappe.cache().make_key(f"ig_mention_reply:{doc.name}")))

		# Rerun once token is configured
		with patch("msuite_core.services.connections.tokens.get_valid_token", return_value="valid_token_now"), \
			patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", return_value="reply_ok") as mock_reply:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "sent")
			mock_reply.assert_called_once()

	@patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_unexpected_error_releases_lock(self, mock_ig_login, mock_token):
		"""Unexpected graph error propagates and releases dedupe lock so rerun can post."""
		doc = self._make_mention()
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Thanks @{{ username }}")
		ctx = self._make_ctx(doc)

		with patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", side_effect=RuntimeError("Unexpected connection error")):
			with self.assertRaises(RuntimeError):
				steps.execute(ctx, step)

		self.assertFalse(frappe.db.exists(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}))
		self.assertIsNone(frappe.cache().get(frappe.cache().make_key(f"ig_mention_reply:{doc.name}")))

		with patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", return_value="reply_ok") as mock_reply:
			res = steps.execute(ctx, step)
			self.assertEqual(res, "sent")
			mock_reply.assert_called_once()

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply", return_value="reply_literal")
	@patch("msuite_core.services.connections.tokens.get_valid_token", return_value="fake_token")
	@patch("msuite_core.integrations.meta.graph.uses_instagram_login", return_value=False)
	def test_public_reply_username_template_literal(self, mock_ig_login, mock_token, mock_reply):
		doc = self._make_mention(username="{{ 7*7 }}")
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Hi @{{ username }}!")
		ctx = self._make_ctx(doc)

		res = steps.execute(ctx, step)
		self.assertEqual(res, "sent")
		mock_reply.assert_called_once()
		self.assertEqual(mock_reply.call_args.kwargs["message"], "Hi @{{ 7*7 }}!")

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply")
	def test_public_reply_malformed_template(self, mock_reply):
		doc = self._make_mention()
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Hi {{ invalid template")
		ctx = self._make_ctx(doc)

		with self.assertRaises(ValueError) as cm:
			steps.execute(ctx, step)
		self.assertIn("Invalid template", str(cm.exception))
		mock_reply.assert_not_called()
		self.assertFalse(frappe.db.exists(IG_CONSENT_DOCTYPE, {"social_mention": doc.name}))
		self.assertIsNone(frappe.cache().get(frappe.cache().make_key(f"ig_mention_reply:{doc.name}")))

	def test_public_reply_wrong_doctype(self):
		todo = frappe.get_doc({"doctype": "ToDo", "description": "not a mention"}).insert(ignore_permissions=True)
		self._created_docs.append((todo.doctype, todo.name))
		step = self._make_step(SEND_IG_PUBLIC_REPLY, ig_reply_text="Hi")
		ctx = self._make_ctx(todo)

		with self.assertRaises(ValueError) as cm:
			steps.execute(ctx, step)
		self.assertIn("Social Mention", str(cm.exception))

	@patch("msuite_inbox.inbox.services.ig_consent.send_consent_template", return_value={"ok": True, "sent": ["CR-1"]})
	def test_consent_template_success(self, mock_send):
		doc = self._make_mention()
		consent = frappe.get_doc({
			"doctype": IG_CONSENT_DOCTYPE,
			"social_mention": doc.name,
			"social_account": self.account_name,
			"interaction_type": doc.interaction_type,
			"post_id": doc.post_id,
			"username": doc.username,
			"status": IG_CONSENT_DM_RECEIVED,
			"conversation": "CONV-1",
			"expires_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), days=7),
		}).insert(ignore_permissions=True, ignore_links=True)
		self._created_docs.append((consent.doctype, consent.name))

		step = self._make_step(
			SEND_IG_CONSENT_TEMPLATE,
			ig_template_title="Can we feature this?",
			ig_template_subtitle="Credit to you",
			ig_yes_label="Yes",
			ig_no_label="No",
			ig_view_label="View",
			ig_image_source="post",
		)
		ctx = self._make_ctx(doc)

		res = steps.execute(ctx, step)
		self.assertEqual(res, "sent")
		mock_send.assert_called_once_with(
			[consent.name],
			{
				"title": "Can we feature this?",
				"subtitle": "Credit to you",
				"yes_label": "Yes",
				"no_label": "No",
				"view_label": "View",
				"image_source": "post",
			},
		)

	@patch("msuite_inbox.inbox.services.ig_consent.send_consent_template")
	def test_consent_template_no_record(self, mock_send):
		doc = self._make_mention()
		step = self._make_step(
			SEND_IG_CONSENT_TEMPLATE,
			ig_template_title="Title",
			ig_yes_label="Yes",
			ig_no_label="No",
			ig_view_label="View",
		)
		ctx = self._make_ctx(doc)

		res = steps.execute(ctx, step)
		self.assertEqual(res, "skipped: no consent record")
		mock_send.assert_not_called()

	@patch("msuite_inbox.inbox.services.ig_consent.send_consent_template", return_value={"ok": False, "reason": "records have no linked conversation"})
	def test_consent_template_not_ok(self, mock_send):
		doc = self._make_mention()
		consent = frappe.get_doc({
			"doctype": IG_CONSENT_DOCTYPE,
			"social_mention": doc.name,
			"social_account": self.account_name,
			"interaction_type": doc.interaction_type,
			"post_id": doc.post_id,
			"username": doc.username,
			"status": IG_CONSENT_DM_RECEIVED,
			"conversation": "CONV-1",
			"expires_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), days=7),
		}).insert(ignore_permissions=True, ignore_links=True)
		self._created_docs.append((consent.doctype, consent.name))

		step = self._make_step(
			SEND_IG_CONSENT_TEMPLATE,
			ig_template_title="Title",
			ig_yes_label="Yes",
			ig_no_label="No",
			ig_view_label="View",
		)
		ctx = self._make_ctx(doc)

		res = steps.execute(ctx, step)
		self.assertEqual(res, "skipped: records have no linked conversation")

	@patch("msuite_inbox.inbox.services.ig_consent.send_consent_template", side_effect=RuntimeError("Meta socket error"))
	def test_consent_template_unexpected_exception(self, mock_send):
		doc = self._make_mention()
		consent = frappe.get_doc({
			"doctype": IG_CONSENT_DOCTYPE,
			"social_mention": doc.name,
			"social_account": self.account_name,
			"interaction_type": doc.interaction_type,
			"post_id": doc.post_id,
			"username": doc.username,
			"status": IG_CONSENT_DM_RECEIVED,
			"conversation": "CONV-1",
			"expires_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), days=7),
		}).insert(ignore_permissions=True, ignore_links=True)
		self._created_docs.append((consent.doctype, consent.name))

		step = self._make_step(
			SEND_IG_CONSENT_TEMPLATE,
			ig_template_title="Title",
			ig_yes_label="Yes",
			ig_no_label="No",
			ig_view_label="View",
		)
		ctx = self._make_ctx(doc)

		res = steps.execute(ctx, step)
		self.assertIn("skipped: RuntimeError: Meta socket error", res)

		consent.reload()
		self.assertEqual(consent.status, IG_CONSENT_FAILED)
		self.assertEqual(consent.failure_reason, "RuntimeError: Meta socket error")

	def _make_dm_received_consent(self, doc):
		"""Insert a DM Received consent record for a mention and track it for cleanup."""
		consent = frappe.get_doc({
			"doctype": IG_CONSENT_DOCTYPE,
			"social_mention": doc.name,
			"social_account": self.account_name,
			"interaction_type": doc.interaction_type,
			"post_id": doc.post_id,
			"username": doc.username,
			"status": IG_CONSENT_DM_RECEIVED,
			"conversation": "CONV-1",
			"expires_at": frappe.utils.add_to_date(frappe.utils.now_datetime(), days=7),
		}).insert(ignore_permissions=True, ignore_links=True)
		self._created_docs.append((consent.doctype, consent.name))
		return consent

	def _card_step(self, **extra):
		return self._make_step(
			SEND_IG_CONSENT_TEMPLATE,
			ig_template_title="Title", ig_yes_label="Yes", ig_no_label="No", ig_view_label="View",
			**extra,
		)

	@patch("msuite_inbox.inbox.services.ig_consent.send_consent_template", return_value={"ok": True})
	def test_consent_template_second_run_does_not_send_again(self, mock_send):
		doc = self._make_mention()
		self._make_dm_received_consent(doc)
		ctx = self._make_ctx(doc)
		self.assertEqual(steps.execute(ctx, self._card_step()), "sent")
		self.assertEqual(steps.execute(ctx, self._card_step()), "skipped: duplicate")
		mock_send.assert_called_once()

	@patch("msuite_inbox.inbox.services.ig_consent.send_consent_template", return_value={"ok": False, "reason": "nope"})
	def test_consent_template_failed_send_releases_lock(self, mock_send):
		doc = self._make_mention()
		self._make_dm_received_consent(doc)
		ctx = self._make_ctx(doc)
		self.assertEqual(steps.execute(ctx, self._card_step()), "skipped: nope")
		self.assertEqual(steps.execute(ctx, self._card_step()), "skipped: nope")
		self.assertEqual(mock_send.call_count, 2)

	def test_validation_image_source(self):
		T = {"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Social Mention"}
		E = {"source_id": "t1", "source_handle": "", "target_id": "s1"}
		base = {"step_id": "s1", "step_type": SEND_IG_CONSENT_TEMPLATE,
			"ig_template_title": "T", "ig_yes_label": "Y", "ig_no_label": "N", "ig_view_label": "V"}
		for ok in ("", "post", "https://cdn.example.com/a.jpg", "http://cdn.example.com/a.jpg"):
			errs = validate_graph([T], [{**base, "ig_image_source": ok}], [E])
			self.assertFalse(any("image" in e for e in errs), (ok, errs))
		for bad in ("ftp://x.com/a.jpg", "javascript:alert(1)", "not a url", "https://", "//x.com/a.jpg"):
			errs = validate_graph([T], [{**base, "ig_image_source": bad}], [E])
			self.assertTrue(any("image must be" in e for e in errs), (bad, errs))

	def test_preview_matches_runtime_jinja_context(self):
		"""Card templates only see username/post_type (as at runtime); labels and subtitle are rendered."""
		doc = self._make_mention(username="ctxuser")
		auto = frappe.get_doc({
			"doctype": "Messaging Automation",
			"title": "Preview Ctx Test",
			"triggers": [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Social Mention", "doc_event": "After Save"}],
			"steps": [{
				"step_id": "s1", "step_type": SEND_IG_CONSENT_TEMPLATE,
				"ig_template_title": "Hi {{ username }}", "ig_template_subtitle": "About {{ doc.name }} {{ post_type }}",
				"ig_yes_label": "Yes {{ username }}", "ig_no_label": "No {{ bad", "ig_view_label": "View {{ post_type }}",
			}],
			"edges": [{"source_id": "t1", "source_handle": "", "target_id": "s1"}],
		})
		row = preview.dry_run(auto, doc)["steps"][0]
		self.assertEqual(row["title"], "Hi ctxuser")
		self.assertEqual(row["yes_label"], "Yes ctxuser")
		self.assertEqual(row["view_label"], "View comment")
		self.assertIn("invalid template", row["no_label"].lower())
		# doc is not in the runtime card context: renders empty (Jinja undefined), never the real name.
		self.assertNotIn(doc.name, row["subtitle"])

	def test_validation(self):
		T_mention = lambda tid: {"trigger_id": tid, "trigger_type": "Document Event", "reference_doctype": "Social Mention"}
		T_todo = lambda tid: {"trigger_id": tid, "trigger_type": "Document Event", "reference_doctype": "ToDo"}
		E = lambda a, b: {"source_id": a, "source_handle": "", "target_id": b}

		# 1. Wrong trigger doctype
		errs = validate_graph([T_todo("t1")], [{"step_id": "s1", "step_type": SEND_IG_PUBLIC_REPLY, "ig_reply_text": "Hi"}], [E("t1", "s1")])
		self.assertTrue(any("Social Mention" in e for e in errs), errs)

		errs = validate_graph([T_todo("t1")], [{
			"step_id": "s1", "step_type": SEND_IG_CONSENT_TEMPLATE,
			"ig_template_title": "T", "ig_yes_label": "Y", "ig_no_label": "N", "ig_view_label": "V",
		}], [E("t1", "s1")])
		self.assertTrue(any("Social Mention" in e for e in errs), errs)

		# 2. Public reply missing text or invalid expiry
		errs = validate_graph([T_mention("t1")], [{"step_id": "s1", "step_type": SEND_IG_PUBLIC_REPLY, "ig_reply_text": ""}], [E("t1", "s1")])
		self.assertTrue(any("reply text is required" in e for e in errs), errs)

		errs = validate_graph([T_mention("t1")], [{"step_id": "s1", "step_type": SEND_IG_PUBLIC_REPLY, "ig_reply_text": "Hi", "ig_expiry_days": 0}], [E("t1", "s1")])
		self.assertTrue(any("expiry days" in e for e in errs), errs)

		# 3. Consent template missing required fields
		errs = validate_graph([T_mention("t1")], [{"step_id": "s1", "step_type": SEND_IG_CONSENT_TEMPLATE}], [E("t1", "s1")])
		self.assertTrue(any("template title is required" in e for e in errs), errs)
		self.assertTrue(any("yes label is required" in e for e in errs), errs)
		self.assertTrue(any("no label is required" in e for e in errs), errs)
		self.assertTrue(any("view label is required" in e for e in errs), errs)

		# 4. Consent template oversize fields
		errs = validate_graph([T_mention("t1")], [{
			"step_id": "s1", "step_type": SEND_IG_CONSENT_TEMPLATE,
			"ig_template_title": "x" * (MESSENGER_TEMPLATE_TITLE_MAX + 1),
			"ig_template_subtitle": "x" * (MESSENGER_TEMPLATE_SUBTITLE_MAX + 1),
			"ig_yes_label": "x" * (MESSENGER_BUTTON_TITLE_MAX + 1),
			"ig_no_label": "N", "ig_view_label": "V",
		}], [E("t1", "s1")])
		self.assertTrue(any("template title exceeds" in e for e in errs), errs)
		self.assertTrue(any("template subtitle exceeds" in e for e in errs), errs)
		self.assertTrue(any("yes label exceeds" in e for e in errs), errs)

	def test_warning_template_step_without_upstream_wait(self):
		T_mention = {"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Social Mention"}
		E = lambda a, b, h="": {"source_id": a, "source_handle": h, "target_id": b}
		template_step = {
			"step_id": "s2", "step_type": SEND_IG_CONSENT_TEMPLATE,
			"ig_template_title": "T", "ig_yes_label": "Y", "ig_no_label": "N", "ig_view_label": "V",
		}

		# Without upstream Wait for Event on IG_CONSENT_DOCTYPE
		warns = validate_warnings([T_mention], [template_step], [E("t1", "s2")])
		self.assertEqual(len(warns), 1)
		self.assertIn("Wait for Event", warns[0])

		# With upstream Wait for Event on IG_CONSENT_DOCTYPE
		wait_step = {
			"step_id": "w1", "step_type": WAIT_EVENT, "wait_value": 7, "wait_unit": "Days",
			"check_doctype": IG_CONSENT_DOCTYPE, "check_link_field": "social_mention",
		}
		warns_ok = validate_warnings([T_mention], [wait_step, template_step], [E("t1", "w1"), E("w1", "s2", "event")])
		self.assertEqual(len(warns_ok), 0)

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply")
	@patch("msuite_inbox.inbox.services.ig_consent.send_consent_template")
	def test_preview(self, mock_send, mock_reply):
		doc = self._make_mention(username="previewuser")
		auto = frappe.get_doc({
			"doctype": "Messaging Automation",
			"title": "Preview Test",
			"triggers": [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Social Mention", "doc_event": "After Save"}],
			"steps": [
				{"step_id": "s1", "step_type": SEND_IG_PUBLIC_REPLY, "ig_reply_text": "Hello @{{ username }}"},
				{"step_id": "s2", "step_type": SEND_IG_CONSENT_TEMPLATE, "ig_template_title": "Can we share?", "ig_yes_label": "Yes", "ig_no_label": "No", "ig_view_label": "View"},
			],
			"edges": [
				{"source_id": "t1", "source_handle": "", "target_id": "s1"},
				{"source_id": "s1", "source_handle": "", "target_id": "s2"},
			],
		})

		initial_consent_count = frappe.db.count(IG_CONSENT_DOCTYPE)
		out = preview.dry_run(auto, doc)

		mock_reply.assert_not_called()
		mock_send.assert_not_called()
		self.assertEqual(frappe.db.count(IG_CONSENT_DOCTYPE), initial_consent_count)

		step_outputs = {s["step_id"]: s for s in out["steps"]}
		self.assertIn("s1", step_outputs)
		self.assertEqual(step_outputs["s1"]["reply_text"], "Hello @previewuser")
		self.assertIn("s2", step_outputs)
		self.assertIn("Can we share?", step_outputs["s2"]["title"])

	@patch("msuite_inbox.inbox.services.mentions.graph.post_mention_reply")
	@patch("msuite_inbox.inbox.services.ig_consent.send_consent_template")
	def test_preview_malformed_template(self, mock_send, mock_reply):
		"""Preview with malformed ig_reply_text shows the invalid-template note, no Meta call."""
		doc = self._make_mention(username="previewuser")
		auto = frappe.get_doc({
			"doctype": "Messaging Automation",
			"title": "Preview Malformed Test",
			"triggers": [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Social Mention", "doc_event": "After Save"}],
			"steps": [
				{"step_id": "s1", "step_type": SEND_IG_PUBLIC_REPLY, "ig_reply_text": "Hello @{{ unclosed"},
				{"step_id": "s2", "step_type": SEND_IG_CONSENT_TEMPLATE, "ig_template_title": "Can we {{ unclosed", "ig_yes_label": "Yes", "ig_no_label": "No", "ig_view_label": "View"},
			],
			"edges": [
				{"source_id": "t1", "source_handle": "", "target_id": "s1"},
				{"source_id": "s1", "source_handle": "", "target_id": "s2"},
			],
		})

		out = preview.dry_run(auto, doc)
		mock_reply.assert_not_called()
		mock_send.assert_not_called()

		step_outputs = {s["step_id"]: s for s in out["steps"]}
		self.assertIn("s1", step_outputs)
		self.assertIn("invalid template", step_outputs["s1"]["reply_text"].lower())
		self.assertIn("s2", step_outputs)
		self.assertIn("invalid template", step_outputs["s2"]["title"].lower())

	def test_preview_post_type_context(self):
		"""Preview renders correct post_type from mention's interaction_type."""
		comment_doc = self._make_mention(interaction_type="comment_mention", username="user1")
		caption_doc = self._make_mention(interaction_type="caption_mention", username="user2", post_id="post_2", comment_id=None)

		auto = frappe.get_doc({
			"doctype": "Messaging Automation",
			"title": "Preview Post Type Test",
			"triggers": [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Social Mention", "doc_event": "After Save"}],
			"steps": [
				{"step_id": "s1", "step_type": SEND_IG_CONSENT_TEMPLATE, "ig_template_title": "Feature this {{ post_type }}?", "ig_yes_label": "Yes", "ig_no_label": "No", "ig_view_label": "View"},
			],
			"edges": [
				{"source_id": "t1", "source_handle": "", "target_id": "s1"},
			],
		})

		out_comment = preview.dry_run(auto, comment_doc)
		out_caption = preview.dry_run(auto, caption_doc)

		self.assertEqual(out_comment["steps"][0]["title"], "Feature this comment?")
		self.assertEqual(out_caption["steps"][0]["title"], "Feature this post?")

	def test_json_expiry_days_default_matches_constant(self):
		"""Verify automation_step.json default for ig_expiry_days matches IG_CONSENT_DEFAULT_EXPIRY_DAYS."""
		from pathlib import Path
		step_json_path = (
			Path(__file__).parent.parent
			/ "doctype"
			/ "automation_step"
			/ "automation_step.json"
		)
		with open(step_json_path) as f:
			data = json.load(f)
		fields = {f["fieldname"]: f for f in data.get("fields", [])}
		self.assertIn("ig_expiry_days", fields)
		self.assertEqual(int(fields["ig_expiry_days"]["default"]), IG_CONSENT_DEFAULT_EXPIRY_DAYS)
