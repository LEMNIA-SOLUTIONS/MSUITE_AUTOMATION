import json
import unittest
from unittest.mock import patch

import frappe

from msuite_automation.msuite_automation.api import automations as api
from msuite_automation.msuite_automation.constants import (
	NOTIFY_APP,
	NOTIFY_DEFAULT_CATEGORY,
	NOTIFY_DEFAULT_PRIORITY,
	NOTIFY_DEFAULT_ROUTE,
	NOTIFY_ERROR_EMPTY_TITLE,
	NOTIFY_MODE_ASSIGNEE,
	NOTIFY_MODE_FIELD,
	NOTIFY_MODE_RECORD_OWNER,
	NOTIFY_MODE_ROLE,
	NOTIFY_MODE_SPECIFIC_USERS,
	NOTIFY_PUSH_ERROR_LOG_TITLE,
	NOTIFY_TITLE_MAX,
	RESULT_SENT,
	RESULT_SKIPPED_ALREADY_SENT,
	RESULT_SKIPPED_DUPLICATE,
	RESULT_SKIPPED_NO_RECIPIENT,
	RESULT_SKIPPED_PUSH_UNAVAILABLE,
	RUN_DOCTYPE,
	RUN_STATUS_COMPLETED,
	RUN_STATUS_FAILED,
	SEND_NOTIFICATION,
	SKIP_REASON_ALREADY_SENT,
	SKIP_REASON_PUSH_UNAVAILABLE,
	STEP_OUTCOME_KEY,
	STEP_STATUS_ERROR,
	STEP_STATUS_SENT,
	STEP_STATUS_SKIPPED,
	UPDATE_FIELD,
)
from msuite_automation.msuite_automation.engine import channels, executor, notify, preview, steps

PUSH_PATH = "msuite_notification.services.queue.enqueue_push"
IN_APP_PATH = "msuite_core.services.notifications.create_notification_log"
ROLE_PREFIX = "ZZ Notify Role"
USER_DOMAIN = "zznotify.example.com"
AUTO_TITLE = "ZZ notify test"


def _ensure_role(role_name: str) -> str:
	if not frappe.db.exists("Role", role_name):
		frappe.get_doc({"doctype": "Role", "role_name": role_name}).insert(ignore_permissions=True)
	return role_name


def _ensure_user(local_part: str, enabled: int = 1, roles: list[str] | None = None) -> str:
	email = f"{local_part}@{USER_DOMAIN}"
	if frappe.db.exists("User", email):
		doc = frappe.get_doc("User", email)
		doc.enabled = enabled
		doc.roles = []
		for r in roles or []:
			doc.append("roles", {"role": r})
		doc.flags.ignore_permissions = True
		doc.save(ignore_permissions=True)
		return email
	doc = frappe.get_doc(
		{
			"doctype": "User",
			"email": email,
			"first_name": local_part,
			"enabled": enabled,
			"send_welcome_email": 0,
			"user_type": "System User",
			"roles": [{"role": r} for r in (roles or [])],
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return email


def _make_todo(owner: str = "Administrator", allocated_to: str | None = None, status: str = "Open", **kw):
	doc = frappe.get_doc(
		{
			"doctype": "ToDo",
			"description": "ZZ notify todo",
			"owner": owner,
			"allocated_to": allocated_to,
			"status": status,
			**kw,
		}
	)
	doc.flags.ignore_permissions = True
	doc.insert(ignore_permissions=True)
	return doc


def _make_automation(step_rows: list[dict], edges: list[tuple[str, str, str]], send_frequency: str = "Every time") -> str:
	doc = frappe.get_doc(
		{
			"doctype": "Messaging Automation",
			"title": AUTO_TITLE,
			"disabled": 1,
			"send_frequency": send_frequency,
			"triggers": [
				{
					"trigger_id": "t1",
					"trigger_type": "Document Event",
					"reference_doctype": "ToDo",
					"doc_event": "After Save",
				}
			],
			"steps": step_rows,
			"edges": [{"source_id": a, "source_handle": h, "target_id": b} for a, h, b in edges],
		}
	)
	doc.flags.ignore_plan_gate = True
	doc.flags.ignore_validate = True
	doc.insert(ignore_permissions=True, ignore_links=True)
	frappe.db.set_value("Messaging Automation", doc.name, "disabled", 0)
	return doc.name


def _step(**kw) -> frappe._dict:
	base = {
		"step_id": "n1",
		"step_type": SEND_NOTIFICATION,
		"notify_push": 1,
		"notify_in_app": 1,
		"notify_mode": NOTIFY_MODE_SPECIFIC_USERS,
		"notify_role": "",
		"notify_users": "[]",
		"notify_field": "",
		"notify_title": "Alert: {{ doc.name }}",
		"notify_body": "Body for {{ doc.description }}",
		"notify_category": NOTIFY_DEFAULT_CATEGORY,
		"notify_priority": NOTIFY_DEFAULT_PRIORITY,
		"notify_route": "/todos/{{ doc.name }}",
	}
	base.update(kw)
	return frappe._dict(base)


def _ctx(step: frappe._dict, doc, automation_name: str = "MA-ZZ-NOTIFY", value: str = "val1", discriminator: str = "") -> frappe._dict:
	return frappe._dict(
		automation=frappe._dict(name=automation_name, send_frequency="Every time"),
		doc=doc,
		value=value,
		discriminator=discriminator,
		entry={},
	)


class TestNotifyStep(unittest.TestCase):
	def setUp(self):
		self._lock_keys: list[str] = []
		self._cleanup()

	def tearDown(self):
		for key in self._lock_keys:
			channels.release_lock(key)
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": AUTO_TITLE}, pluck="name"):
			frappe.db.delete(RUN_DOCTYPE, {"automation": name})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "ZZ notify%")})
		frappe.db.delete("Notification Log", {"subject": ("like", "ZZ notify%")})
		for user in frappe.get_all("User", filters={"email": ("like", f"%@{USER_DOMAIN}")}, pluck="name"):
			frappe.delete_doc("User", user, force=True, ignore_permissions=True)
		for role in frappe.get_all("Role", filters={"role_name": ("like", f"{ROLE_PREFIX}%")}, pluck="name"):
			frappe.delete_doc("Role", role, force=True, ignore_permissions=True)

	def _track_lock(self, ctx, step):
		key = steps._dedupe_key(ctx, step, "notify")
		self._lock_keys.append(key)
		return key

	def test_role_mode_returns_only_enabled_users(self):
		role = _ensure_role(f"{ROLE_PREFIX} A")
		enabled_u = _ensure_user("role_enabled", enabled=1, roles=[role])
		_ensure_user("role_disabled", enabled=0, roles=[role])
		_ensure_user("other_role_user", enabled=1, roles=[])

		step = _step(notify_mode=NOTIFY_MODE_ROLE, notify_role=role)
		doc = frappe._dict(doctype="ToDo", name="T-1")
		self.assertEqual(notify.resolve_notify_recipients(step, doc), [enabled_u])

	def test_specific_users_ignores_unknown_and_disabled(self):
		u1 = _ensure_user("spec1", enabled=1)
		u2 = _ensure_user("spec2", enabled=1)
		disabled = _ensure_user("spec_off", enabled=0)

		step = _step(
			notify_mode=NOTIFY_MODE_SPECIFIC_USERS,
			notify_users=json.dumps([u2, disabled, "unknown@nowhere.invalid", u1, u2]),
		)
		doc = frappe._dict(doctype="ToDo", name="T-2")
		self.assertEqual(notify.resolve_notify_recipients(step, doc), [u2, u1])

	def test_owner_mode(self):
		u1 = _ensure_user("owner_on", enabled=1)
		u_off = _ensure_user("owner_off", enabled=0)
		step = _step(notify_mode=NOTIFY_MODE_RECORD_OWNER)

		self.assertEqual(notify.resolve_notify_recipients(step, frappe._dict(doctype="ToDo", name="T-3", owner=u1)), [u1])
		self.assertEqual(notify.resolve_notify_recipients(step, frappe._dict(doctype="ToDo", name="T-3", owner=u_off)), [])

	def test_assignee_mode_uses_open_todos_only(self):
		u_open = _ensure_user("assign_open", enabled=1)
		u_closed = _ensure_user("assign_closed", enabled=1)
		u_cancelled = _ensure_user("assign_cancel", enabled=1)
		u_disabled = _ensure_user("assign_off", enabled=0)

		target = _make_todo()
		_make_todo(reference_type="ToDo", reference_name=target.name, allocated_to=u_open, status="Open")
		_make_todo(reference_type="ToDo", reference_name=target.name, allocated_to=u_closed, status="Closed")
		_make_todo(reference_type="ToDo", reference_name=target.name, allocated_to=u_cancelled, status="Cancelled")
		_make_todo(reference_type="ToDo", reference_name=target.name, allocated_to=u_disabled, status="Open")

		step = _step(notify_mode=NOTIFY_MODE_ASSIGNEE)
		self.assertEqual(notify.resolve_notify_recipients(step, target), [u_open])

	def test_field_mode_splits_comma_and_semicolon_lists(self):
		step = _step(notify_mode=NOTIFY_MODE_FIELD, notify_field="custom_recipients")
		doc = frappe._dict(
			doctype="ToDo",
			name="T-5",
			custom_recipients="a@x.com; b@x.com, a@x.com ; c@x.com ",
		)
		self.assertEqual(notify.resolve_notify_recipients(step, doc), ["a@x.com", "b@x.com", "c@x.com"])

	@patch(PUSH_PATH, return_value=["PNL-1"])
	@patch(IN_APP_PATH)
	def test_field_mode_keeps_non_user_email_push_only(self, mock_in_app, mock_push):
		u1 = _ensure_user("field_user", enabled=1)
		non_user = "external_person@outside.invalid"
		step = _step(
			notify_mode=NOTIFY_MODE_FIELD,
			notify_field="custom_recipients",
			notify_push=1,
			notify_in_app=1,
			notify_title="ZZ notify field",
		)
		doc = frappe._dict(doctype="ToDo", name="T-6", description="d", custom_recipients=f"{u1}, {non_user}")
		ctx = _ctx(step, doc, value="v-field")
		self._track_lock(ctx, step)

		self.assertEqual(notify.resolve_notify_recipients(step, doc), [u1, non_user])

		res = notify.send_notification(ctx, step)
		self.assertEqual(res, RESULT_SENT)
		mock_in_app.assert_called_once()
		self.assertEqual(mock_in_app.call_args.kwargs["recipient"], u1)
		self.assertEqual(mock_push.call_count, 2)
		self.assertEqual([c.kwargs["recipient"] for c in mock_push.call_args_list], [u1, non_user])

	def test_guest_and_blank_dropped(self):
		u1 = _ensure_user("valid_user", enabled=1)
		spec_step = _step(
			notify_mode=NOTIFY_MODE_SPECIFIC_USERS,
			notify_users=json.dumps(["", "   ", None, "Guest", u1]),
		)
		field_step = _step(notify_mode=NOTIFY_MODE_FIELD, notify_field="custom_recipients")
		doc = frappe._dict(doctype="ToDo", name="T-7", custom_recipients=f"Guest, ,   ; {u1}")

		self.assertEqual(notify.resolve_notify_recipients(spec_step, doc), [u1])
		self.assertEqual(notify.resolve_notify_recipients(field_step, doc), [u1])

	@patch(PUSH_PATH)
	@patch(IN_APP_PATH)
	def test_no_recipients_is_skipped_no_recipient(self, mock_in_app, mock_push):
		step = _step(notify_mode=NOTIFY_MODE_SPECIFIC_USERS, notify_users="[]")
		doc = frappe._dict(doctype="ToDo", name="T-8", description="d")
		ctx = _ctx(step, doc, value="v-empty")
		self._track_lock(ctx, step)

		res = steps.execute(ctx, step)
		self.assertEqual(res, RESULT_SKIPPED_NO_RECIPIENT)
		self.assertEqual(res.outcome_status, "skipped")
		self.assertEqual(res.reason, "no recipient")
		mock_in_app.assert_not_called()
		mock_push.assert_not_called()

	@patch(PUSH_PATH)
	@patch(IN_APP_PATH)
	def test_empty_rendered_title_is_step_error(self, mock_in_app, mock_push):
		u1 = _ensure_user("empty_title", enabled=1)
		for bad_title in ("", "   ", "{{ doc.nonexistent_field }}"):
			step = _step(notify_users=json.dumps([u1]), notify_title=bad_title)
			doc = frappe._dict(doctype="ToDo", name="T-9", description="d")
			ctx = _ctx(step, doc, value="v-title")
			res = steps.execute(ctx, step)
			self.assertEqual(res.outcome_status, STEP_STATUS_ERROR)
		mock_in_app.assert_not_called()
		mock_push.assert_not_called()

	def test_title_truncated_to_140(self):
		long_title = "X" * 200
		step = _step(notify_title=long_title, notify_body="b", notify_route="")
		rendered = notify.render_notify(step, frappe._dict(doctype="ToDo", name="T-10"))
		self.assertEqual(len(rendered["title"]), NOTIFY_TITLE_MAX)
		self.assertEqual(rendered["title"], "X" * NOTIFY_TITLE_MAX)
		self.assertEqual(rendered["route"], NOTIFY_DEFAULT_ROUTE)

	def test_unicode_and_emoji_title(self):
		prefix = "🚀 "
		text = prefix + ("नमस्ते " * 40)
		step = _step(notify_title=text, notify_body="🎉 {{ doc.name }}", notify_route="/app")
		rendered = notify.render_notify(step, frappe._dict(doctype="ToDo", name="T-11"))
		self.assertEqual(len(rendered["title"]), NOTIFY_TITLE_MAX)
		self.assertTrue(rendered["title"].startswith(prefix))
		self.assertEqual(rendered["body"], "🎉 T-11")

	def test_jinja_missing_field_renders_blank(self):
		step = _step(notify_title="Hi {{ doc.name }}", notify_body="Missing:[{{ doc.does_not_exist }}]")
		rendered = notify.render_notify(step, frappe._dict(doctype="ToDo", name="T-12"))
		self.assertEqual(rendered["title"], "Hi T-12")
		self.assertEqual(rendered["body"], "Missing:[]")

	def test_invalid_jinja_gives_readable_error(self):
		step = _step(notify_title="Broken {{ doc.name", notify_body="ok")
		doc = frappe._dict(doctype="ToDo", name="T-13", description="d")
		with self.assertRaises(frappe.ValidationError) as err:
			notify.render_notify(step, doc)
		self.assertIn("template", str(err.exception).lower())

		res = steps.execute(_ctx(step, doc, value="v-jinja"), step)
		self.assertEqual(res.outcome_status, STEP_STATUS_ERROR)
		self.assertIn("template", res.reason.lower())

	@patch("frappe.publish_realtime")
	@patch(PUSH_PATH)
	def test_in_app_only_never_calls_push(self, mock_push, mock_realtime):
		u1 = _ensure_user("in_app_only", enabled=1)
		step = _step(
			notify_push=0,
			notify_in_app=1,
			notify_users=json.dumps([u1]),
			notify_title="ZZ notify bell {{ doc.name }}",
			notify_body="Bell body",
			notify_route="/custom/{{ doc.name }}",
		)
		doc = frappe._dict(doctype="ToDo", name="T-14", description="desc")
		ctx = _ctx(step, doc, value="v-inapp")
		self._track_lock(ctx, step)

		res = steps.execute(ctx, step)
		self.assertEqual(res, RESULT_SENT)
		mock_push.assert_not_called()

		log_row = frappe.db.get_value(
			"Notification Log",
			{"for_user": u1, "document_type": "ToDo", "document_name": "T-14"},
			["subject", "email_content", "msuite_route", "read"],
			as_dict=True,
		)
		self.assertIsNotNone(log_row)
		self.assertEqual(log_row.subject, "ZZ notify bell T-14")
		self.assertEqual(log_row.email_content, "Bell body")
		self.assertEqual(log_row.msuite_route, "/custom/T-14")
		self.assertEqual(log_row.read, 0)
		mock_realtime.assert_called()

	@patch(PUSH_PATH, return_value=["PNL-15"])
	@patch(IN_APP_PATH)
	def test_push_only_never_creates_bell_row(self, mock_in_app, mock_push):
		u1 = _ensure_user("push_only", enabled=1)
		step = _step(
			notify_push=1,
			notify_in_app=0,
			notify_users=json.dumps([u1]),
			notify_title="Push {{ doc.name }}",
			notify_body="Push body",
			notify_category="Billing",
			notify_priority="normal",
			notify_route="/billing/{{ doc.name }}",
		)
		doc = frappe._dict(doctype="ToDo", name="T-15", description="desc")
		ctx = _ctx(step, doc, value="v-push")
		key = self._track_lock(ctx, step)

		res = steps.execute(ctx, step)
		self.assertEqual(res, RESULT_SENT)
		mock_in_app.assert_not_called()
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u1, "document_name": "T-15"}), 0)
		mock_push.assert_called_once_with(
			recipient=u1,
			title="Push T-15",
			body="Push body",
			data={"screen": "/billing/T-15", "entity_type": "ToDo", "entity_name": "T-15"},
			priority="normal",
			category="Billing",
			idempotency_key=f"{key}::{u1}",
			source_doctype="ToDo",
			source_name="T-15",
			enqueue_after_commit=True,
		)

	@patch(PUSH_PATH, return_value=["PNL-16"])
	@patch(IN_APP_PATH)
	def test_both_channels(self, mock_in_app, mock_push):
		u1 = _ensure_user("both_1", enabled=1)
		u2 = _ensure_user("both_2", enabled=1)
		step = _step(
			notify_push=1,
			notify_in_app=1,
			notify_users=json.dumps([u1, u2]),
			notify_title="Both {{ doc.name }}",
			notify_body="Both body",
			notify_route="",
		)
		doc = frappe._dict(doctype="ToDo", name="T-16", description="desc")
		ctx = _ctx(step, doc, value="v-both")
		self._track_lock(ctx, step)

		res = steps.execute(ctx, step)
		self.assertEqual(res, RESULT_SENT)
		self.assertEqual(mock_in_app.call_count, 2)
		self.assertEqual(mock_push.call_count, 2)
		self.assertEqual(mock_in_app.call_args_list[0].kwargs["msuite_route"], NOTIFY_DEFAULT_ROUTE)
		self.assertIsNone(mock_in_app.call_args_list[0].kwargs["route"])

	@patch(PUSH_PATH)
	@patch(IN_APP_PATH)
	def test_app_not_installed_still_sends_in_app(self, mock_in_app, mock_push):
		u1 = _ensure_user("no_app_user", enabled=1)
		doc = frappe._dict(doctype="ToDo", name="T-17", description="d")
		apps_without_notify = [app for app in frappe.get_installed_apps() if app != NOTIFY_APP]

		with patch("frappe.get_installed_apps", return_value=apps_without_notify):
			both_step = _step(notify_push=1, notify_in_app=1, notify_users=json.dumps([u1]))
			ctx1 = _ctx(both_step, doc, value="v-noapp-1")
			self._track_lock(ctx1, both_step)
			self.assertEqual(steps.execute(ctx1, both_step), RESULT_SENT)
			mock_in_app.assert_called_once()
			mock_push.assert_not_called()

			push_only_step = _step(notify_push=1, notify_in_app=0, notify_users=json.dumps([u1]))
			ctx2 = _ctx(push_only_step, doc, value="v-noapp-2")
			self._track_lock(ctx2, push_only_step)
			res = steps.execute(ctx2, push_only_step)
			self.assertEqual(res, RESULT_SKIPPED_PUSH_UNAVAILABLE)
			self.assertEqual(res.outcome_status, "skipped")
			self.assertEqual(res.reason, SKIP_REASON_PUSH_UNAVAILABLE)

	@patch(PUSH_PATH, return_value=[])
	@patch(IN_APP_PATH)
	def test_push_disabled_in_config_is_not_a_failure(self, mock_in_app, mock_push):
		u1 = _ensure_user("cfg_off_user", enabled=1)
		doc = frappe._dict(doctype="ToDo", name="T-18", description="d")

		both_step = _step(notify_push=1, notify_in_app=1, notify_users=json.dumps([u1]))
		ctx1 = _ctx(both_step, doc, value="v-cfgoff-1")
		self._track_lock(ctx1, both_step)
		self.assertEqual(steps.execute(ctx1, both_step), RESULT_SENT)
		mock_in_app.assert_called_once()

		push_only_step = _step(notify_push=1, notify_in_app=0, notify_users=json.dumps([u1]))
		ctx2 = _ctx(push_only_step, doc, value="v-cfgoff-2")
		self._track_lock(ctx2, push_only_step)
		res = steps.execute(ctx2, push_only_step)
		self.assertEqual(res, RESULT_SKIPPED_PUSH_UNAVAILABLE)
		self.assertEqual(res.outcome_status, "skipped")
		self.assertEqual(res.reason, SKIP_REASON_PUSH_UNAVAILABLE)

	@patch(PUSH_PATH, return_value=["PNL-19"])
	@patch(IN_APP_PATH)
	def test_duplicate_run_sends_once(self, mock_in_app, mock_push):
		u1 = _ensure_user("dedupe_user", enabled=1)
		step = _step(notify_users=json.dumps([u1]))
		doc = frappe._dict(doctype="ToDo", name="T-19", description="d")
		ctx = _ctx(step, doc, value="v-dup-1")
		self._track_lock(ctx, step)

		first = steps.execute(ctx, step)
		second = steps.execute(ctx, step)
		self.assertEqual(first, RESULT_SENT)
		self.assertEqual(second, RESULT_SKIPPED_DUPLICATE)
		mock_in_app.assert_called_once()
		mock_push.assert_called_once()

	@patch(PUSH_PATH, return_value=["PNL-20"])
	@patch(IN_APP_PATH)
	def test_retry_run_does_not_double_send(self, mock_in_app, mock_push):
		u1 = _ensure_user("retry_user", enabled=1)
		todo = _make_todo(owner=u1)
		auto_name = _make_automation(
			[
				dict(_step(step_id="n1", notify_users=json.dumps([u1]))),
				{"step_id": "u1", "step_type": UPDATE_FIELD, "update_field": "nonexistent_bad_field", "update_value": "x"},
			],
			[("t1", "", "n1"), ("n1", "", "u1")],
			send_frequency="Every time",
		)
		step_obj = frappe._dict(step_id="n1")
		self._lock_keys.append(
			steps._dedupe_key(
				frappe._dict(automation=frappe._dict(name=auto_name), doc=todo, value="v-retry", discriminator="d1"),
				step_obj,
				"notify",
			)
		)

		with patch.object(steps, "_save_field", side_effect=frappe.ValidationError("bad value")):
			run1 = executor.run_automation(auto_name, "t1", "ToDo", todo.name, value="v-retry", discriminator="d1")
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run1, "status"), "Failed")
		mock_in_app.assert_called_once()
		mock_push.assert_called_once()

		# Retrying the same run context skips the already-sent notification step via the dedupe lock
		run2 = executor.run_automation(auto_name, "t1", "ToDo", todo.name, value="v-retry", discriminator="d1", skip_checks=True, retry=True)
		self.assertIsNotNone(run2)
		log2 = json.loads(frappe.db.get_value(RUN_DOCTYPE, run2, "log"))
		n1_entry = next(e for e in log2 if e["step_id"] == "n1")
		self.assertEqual(n1_entry["result"], RESULT_SKIPPED_DUPLICATE)
		mock_in_app.assert_called_once()
		mock_push.assert_called_once()

	@patch(PUSH_PATH)
	@patch(IN_APP_PATH)
	def test_all_channels_off_is_step_error(self, mock_in_app, mock_push):
		u1 = _ensure_user("all_off", enabled=1)
		step = _step(notify_push=0, notify_in_app=0, notify_users=json.dumps([u1]))
		doc = frappe._dict(doctype="ToDo", name="T-21", description="d")
		res = steps.execute(_ctx(step, doc, value="v-off"), step)
		self.assertEqual(res.outcome_status, STEP_STATUS_ERROR)
		mock_in_app.assert_not_called()
		mock_push.assert_not_called()

	@patch("frappe.log_error")
	@patch("frappe.publish_realtime")
	@patch(PUSH_PATH, side_effect=[RuntimeError("queue down"), ["PNL-2"]])
	def test_push_exception_does_not_fail_step_or_stop_loop(self, mock_push, mock_realtime, mock_log_error):
		u1 = _ensure_user("push_err_1", enabled=1)
		u2 = _ensure_user("push_err_2", enabled=1)
		step = _step(
			notify_push=1,
			notify_in_app=1,
			notify_users=json.dumps([u1, u2]),
			notify_title="ZZ notify push err {{ doc.name }}",
			notify_body="Alert body",
			notify_route="/todos/{{ doc.name }}",
		)
		doc = frappe._dict(doctype="ToDo", name="T-22", description="d")
		ctx = _ctx(step, doc, value="v-push-err")
		self._track_lock(ctx, step)

		res = steps.execute(ctx, step)
		self.assertEqual(res, RESULT_SENT)
		self.assertNotEqual(res.outcome_status, STEP_STATUS_ERROR)
		self.assertEqual(mock_push.call_count, 2)
		self.assertEqual([c.kwargs["recipient"] for c in mock_push.call_args_list], [u1, u2])
		mock_log_error.assert_called_once()
		self.assertEqual(mock_log_error.call_args.kwargs["title"], NOTIFY_PUSH_ERROR_LOG_TITLE)

		for user in (u1, u2):
			row = frappe.db.get_value(
				"Notification Log",
				{"for_user": user, "document_type": "ToDo", "document_name": "T-22"},
				["subject", "msuite_route", "read"],
				as_dict=True,
			)
			self.assertIsNotNone(row, f"Missing in-app Notification Log for {user}")
			self.assertEqual(row.subject, "ZZ notify push err T-22")

	def _preview_automation(self, step_kw: dict):
		return frappe.get_doc(
			{
				"doctype": "Messaging Automation",
				"title": AUTO_TITLE,
				"disabled": 1,
				"triggers": [
					{
						"trigger_id": "t1",
						"trigger_type": "Document Event",
						"reference_doctype": "ToDo",
						"doc_event": "After Save",
					}
				],
				"steps": [dict(_step(**step_kw))],
				"edges": [{"source_id": "t1", "source_handle": "", "target_id": "n1"}],
			}
		)

	def test_preview_lists_recipients_and_rendered_text(self):
		u1 = _ensure_user("prev_1", enabled=1)
		u2 = _ensure_user("prev_2", enabled=1)
		auto = self._preview_automation(
			{
				"notify_push": 1,
				"notify_in_app": 1,
				"notify_mode": NOTIFY_MODE_SPECIFIC_USERS,
				"notify_users": json.dumps([u1, u2]),
				"notify_title": "ZZ notify {{ doc.name }}",
				"notify_body": "Desc: {{ doc.description }}",
				"notify_route": "/todo/{{ doc.name }}",
			}
		)
		doc = frappe._dict(doctype="ToDo", name="T-PREV-1", description="Check order")
		out = preview.dry_run(auto, doc)
		step_row = out["steps"][0]
		self.assertNotIn("error", step_row)
		self.assertEqual(step_row["channels"], ["push", "in_app"])
		self.assertEqual(step_row["to"], [u1, u2])
		self.assertEqual(step_row["title"], "ZZ notify T-PREV-1")
		self.assertEqual(step_row["body"], "Desc: Check order")
		self.assertEqual(step_row["route"], "/todo/T-PREV-1")

	def test_preview_no_recipient_is_skipped(self):
		auto = self._preview_automation(
			{
				"notify_mode": NOTIFY_MODE_SPECIFIC_USERS,
				"notify_users": "[]",
			}
		)
		doc = frappe._dict(doctype="ToDo", name="T-PREV-2", description="d")
		out = preview.dry_run(auto, doc)
		self.assertEqual(out["steps"][0]["skipped"], "no recipient")

	@patch("frappe.publish_realtime")
	@patch(PUSH_PATH)
	@patch(IN_APP_PATH)
	def test_preview_writes_nothing(self, mock_in_app, mock_push, mock_realtime):
		u1 = _ensure_user("prev_nowrite", enabled=1)
		auto = self._preview_automation(
			{
				"notify_push": 1,
				"notify_in_app": 1,
				"notify_mode": NOTIFY_MODE_SPECIFIC_USERS,
				"notify_users": json.dumps([u1]),
				"notify_title": "ZZ notify no write {{ doc.name }}",
			}
		)
		doc = frappe._dict(doctype="ToDo", name="T-PREV-3", description="d")
		bell_before = frappe.db.count("Notification Log")
		has_push_log = frappe.db.exists("DocType", "Push Notification Log")
		push_before = frappe.db.count("Push Notification Log") if has_push_log else 0
		mock_realtime.reset_mock()

		out = preview.dry_run(auto, doc)
		self.assertEqual(out["steps"][0]["to"], [u1])
		mock_in_app.assert_not_called()
		mock_push.assert_not_called()
		mock_realtime.assert_not_called()
		self.assertEqual(frappe.db.count("Notification Log"), bell_before)
		if has_push_log:
			self.assertEqual(frappe.db.count("Push Notification Log"), push_before)

	def test_preview_invalid_jinja_returns_readable_error(self):
		u1 = _ensure_user("prev_bad_jinja", enabled=1)
		auto = self._preview_automation(
			{
				"notify_mode": NOTIFY_MODE_SPECIFIC_USERS,
				"notify_users": json.dumps([u1]),
				"notify_title": "Broken {{ doc.name",
			}
		)
		doc = frappe._dict(doctype="ToDo", name="T-PREV-4", description="d")
		out = preview.dry_run(auto, doc)
		step_row = out["steps"][0]
		self.assertIn("error", step_row)
		self.assertIn("template", step_row["error"].lower())
		self.assertNotIn("Traceback", step_row["error"])

	def test_get_automations_channel_chip_notify(self):
		frappe.set_user("Administrator")
		u1 = _ensure_user("chip_user", enabled=1)
		name = api.save_automation(
			{
				"title": AUTO_TITLE,
				"disabled": 1,
				"triggers": [
					{
						"trigger_id": "t1",
						"trigger_type": "Document Event",
						"reference_doctype": "ToDo",
						"doc_event": "After Save",
					}
				],
				"steps": [
					dict(_step(step_id="n1", notify_users=json.dumps([u1]))),
					{"step_id": "s1", "step_type": "Send SMS", "sms_message": "hi"},
				],
				"edges": [
					{"source_id": "t1", "source_handle": "", "target_id": "n1"},
					{"source_id": "n1", "source_handle": "", "target_id": "s1"},
				],
			}
		)["name"]
		row = next(r for r in api.get_automations(search=AUTO_TITLE)["items"] if r["name"] == name)
		self.assertEqual(row["channels"], ["SMS", "Notify"])

	@patch(PUSH_PATH)
	@patch(IN_APP_PATH)
	def test_restricted_jinja_blocks_dangerous_templates(self, mock_in_app, mock_push):
		u1 = _ensure_user("jinja_sec", enabled=1)
		doc = _make_todo(description="Safe desc")
		dangerous = [
			"{{ frappe.db.sql('select email from tabUser where name=\"Administrator\"') }}",
			"{{ frappe.get_doc('User', 'Administrator').email }}",
			"{{ frappe.get_all('User', pluck='name') }}",
			"{{ doc.__class__ }}",
			"{{ frappe.sendmail() }}",
			"{{ frappe.sendmail }}",
			"{{ frappe.make_post_request('https://evil.example') }}",
			"{{ frappe.make_post_request }}",
		]
		for idx, tpl in enumerate(dangerous):
			with self.subTest(tpl=tpl):
				step = _step(
					notify_users=json.dumps([u1]),
					notify_title=tpl,
					notify_body="safe body",
				)
				ctx = _ctx(step, doc, value=f"v-sec-{idx}")
				res = steps.execute(ctx, step)
				self.assertEqual(res.outcome_status, STEP_STATUS_ERROR)
				self.assertNotIn("Traceback", res.reason or "")
				self.assertNotIn("Administrator", res.reason or "")

				# Also verify in body position nothing sensitive is ever rendered
				body_step = _step(notify_users=json.dumps([u1]), notify_title="Safe", notify_body=tpl)
				try:
					rendered = notify.render_notify(body_step, doc)
				except frappe.ValidationError as err:
					self.assertNotIn("Traceback", str(err))
				else:
					self.assertEqual(rendered["body"], "")
		mock_in_app.assert_not_called()
		mock_push.assert_not_called()

	def test_jinja_cannot_reach_other_records_or_document_methods(self):
		"""Round-2 review: frappe.utils helpers, extra Frappe globals and live Document methods must stay unreachable."""
		todo = frappe.get_doc("ToDo", _make_todo(description="Safe desc").name)
		vectors = [
			"{{ frappe.utils.get_user_info_for_avatar('Administrator') }}",
			"{{ frappe.utils.formatdate('2026-01-02') }}",
			"{{ doc.db_get('owner') }}",
			"{{ doc.get_db_value('owner') }}",
			"{{ doc.reload() }}",
			"{{ doc.as_dict().owner }}",
			"{{ doc|attr('db_get')('owner') }}",
			"{{ doc.db_set('description', 'MUTATED') }}",
			"{{ web_blocks }}{{ get_toc }}{{ get_serial_or_batch_nos }}",
		]
		for tpl in vectors:
			with self.subTest(tpl=tpl):
				step = _step(notify_title=tpl, notify_body="b")
				try:
					title = notify.render_notify(step, todo)["title"]
				except frappe.ValidationError as err:
					self.assertNotIn("Traceback", str(err))
				else:
					self.assertNotIn("Administrator", title)
					self.assertNotIn("@", title)
					self.assertEqual(title, "")
		self.assertEqual(frappe.db.get_value("ToDo", todo.name, "description"), "Safe desc")

	def test_field_mode_matches_user_names_case_insensitively(self):
		"""A record field holding 'Alice@X.com' must reach the user 'alice@x.com' and honour enabled/disabled."""
		on = _ensure_user("case_on", enabled=1)
		off = _ensure_user("case_off", enabled=0)
		doc = frappe._dict(doctype="ToDo", name="X", owner="Administrator", mixed=f"{on.upper()};{off.upper()}")
		step = _step(notify_mode=NOTIFY_MODE_FIELD, notify_field="mixed")
		self.assertEqual(notify.resolve_notify_recipients(step, doc), [on.upper()])
		role_users = notify._users_by_enabled([on.upper(), off.upper()], 1)
		self.assertEqual(role_users, {on.lower()})

	def test_jinja_snapshot_still_renders_record_fields_and_filters(self):
		todo = frappe.get_doc("ToDo", _make_todo(description="Safe desc").name)
		step = _step(notify_title="{{ doc.description|upper }} {{ doc.creation.strftime('%Y') }}", notify_body="{{ range(2)|list }} {{ _('Hello') }}")
		rendered = notify.render_notify(step, todo)
		self.assertEqual(rendered["title"], f"SAFE DESC {todo.creation.strftime('%Y')}")
		self.assertEqual(rendered["body"], "[0, 1] Hello")

	def test_restricted_jinja_allows_normal_fields_and_safe_helpers(self):
		doc = _make_todo(description="Normal desc")
		step = _step(
			notify_title="{{ doc.description }} {{ frappe.format_value(5) }}",
			notify_body="{{ doc.creation }}|[{{ doc.missing_field_xyz }}]",
		)
		rendered = notify.render_notify(step, doc)
		self.assertEqual(rendered["title"], "Normal desc 5")
		self.assertTrue(rendered["body"].startswith(str(doc.creation)))
		self.assertTrue(rendered["body"].endswith("|[]"))

	def test_render_notify_sanitizes_route(self):
		doc = frappe._dict(doctype="ToDo", name="T-ROUTE")
		cases = [
			("/tasks", "/tasks"),
			("/inbox/conv/ABC?x=1#y", "/inbox/conv/ABC?x=1#y"),
			("", NOTIFY_DEFAULT_ROUTE),
			("javascript:alert(1)", NOTIFY_DEFAULT_ROUTE),
			("https://evil.example", NOTIFY_DEFAULT_ROUTE),
			("//evil.example", NOTIFY_DEFAULT_ROUTE),
			("/\\\\evil", NOTIFY_DEFAULT_ROUTE),
			("/a\nb", NOTIFY_DEFAULT_ROUTE),
			(" /tasks", NOTIFY_DEFAULT_ROUTE),
			("tasks", NOTIFY_DEFAULT_ROUTE),
		]
		for raw_route, expected in cases:
			with self.subTest(raw_route=raw_route):
				step = _step(notify_route=raw_route)
				rendered = notify.render_notify(step, doc)
				self.assertEqual(rendered["route"], expected)

	@patch(PUSH_PATH)
	@patch(IN_APP_PATH)
	def test_run_automation_marks_run_failed_on_notify_step_error(self, mock_in_app, mock_push):
		u1 = _ensure_user("run_err_user", enabled=1)
		todo = _make_todo(owner=u1)

		empty_auto = _make_automation(
			[dict(_step(step_id="n1", notify_users=json.dumps([u1]), notify_title="{{ doc.missing_title_field }}"))],
			[("t1", "", "n1")],
		)
		run_id = executor.run_automation(empty_auto, "t1", "ToDo", todo.name)
		self.assertIsNotNone(run_id)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run_id, "status"), RUN_STATUS_FAILED)
		log = json.loads(frappe.db.get_value(RUN_DOCTYPE, run_id, "log"))
		n1_entry = next(e for e in log if e["step_id"] == "n1")
		self.assertEqual(n1_entry.get("outcome_status"), STEP_STATUS_ERROR)
		self.assertEqual(n1_entry.get("error"), NOTIFY_ERROR_EMPTY_TITLE)
		self.assertEqual(api.get_runs(empty_auto)[0]["error"], NOTIFY_ERROR_EMPTY_TITLE)

		bad_jinja_auto = _make_automation(
			[dict(_step(step_id="n1", notify_users=json.dumps([u1]), notify_title="Broken {{ doc.name"))],
			[("t1", "", "n1")],
		)
		bad_run_id = executor.run_automation(bad_jinja_auto, "t1", "ToDo", todo.name)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, bad_run_id, "status"), RUN_STATUS_FAILED)
		bad_log = json.loads(frappe.db.get_value(RUN_DOCTYPE, bad_run_id, "log"))
		bad_entry = next(e for e in bad_log if e["step_id"] == "n1")
		self.assertEqual(bad_entry.get("outcome_status"), STEP_STATUS_ERROR)
		self.assertIn("template", bad_entry.get("error", "").lower())
		self.assertNotIn("Traceback", bad_entry.get("error", ""))

		mock_in_app.assert_not_called()
		mock_push.assert_not_called()

	@patch(PUSH_PATH, return_value=["PNL-OK"])
	@patch(IN_APP_PATH)
	def test_run_automation_completes_on_valid_notify_step(self, mock_in_app, mock_push):
		u1 = _ensure_user("run_ok_user", enabled=1)
		todo = _make_todo(owner=u1)
		auto_name = _make_automation(
			[dict(_step(step_id="n1", notify_users=json.dumps([u1]), notify_title="Hello {{ doc.name }}"))],
			[("t1", "", "n1")],
		)
		run_id = executor.run_automation(auto_name, "t1", "ToDo", todo.name)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run_id, "status"), RUN_STATUS_COMPLETED)
		mock_in_app.assert_called_once()
		mock_push.assert_called_once()

	@patch(PUSH_PATH, return_value=["PNL-KEY"])
	@patch(IN_APP_PATH)
	def test_push_idempotency_key_unique_per_run(self, mock_in_app, mock_push):
		u1 = _ensure_user("idem_user", enabled=1)
		step = _step(notify_push=1, notify_in_app=0, notify_users=json.dumps([u1]))
		doc = frappe._dict(doctype="ToDo", name="T-KEY", description="d")

		ctx_r1 = _ctx(step, doc, value="")
		ctx_r1["run"] = "RUN-001"
		ctx_r1_again = _ctx(step, doc, value="")
		ctx_r1_again["run"] = "RUN-001"
		ctx_r2 = _ctx(step, doc, value="")
		ctx_r2["run"] = "RUN-002"

		notify.send_notification(ctx_r1, step)
		notify.send_notification(ctx_r1_again, step)
		notify.send_notification(ctx_r2, step)

		keys = [c.kwargs["idempotency_key"] for c in mock_push.call_args_list]
		self.assertEqual(len(keys), 3)
		self.assertEqual(keys[0], keys[1])
		self.assertNotEqual(keys[0], keys[2])
		self.assertIn(":RUN-001:", keys[0])
		self.assertIn(":RUN-002:", keys[2])

	def test_field_mode_drops_disabled_users_keeps_non_user_emails(self):
		u_on = _ensure_user("field_on", enabled=1)
		u_off = _ensure_user("field_off", enabled=0)
		non_user = "external_only@outside.invalid"
		step = _step(notify_mode=NOTIFY_MODE_FIELD, notify_field="custom_recipients")
		doc = frappe._dict(
			doctype="ToDo",
			name="T-FIELD-DIS",
			custom_recipients=f"{u_on}, {u_off}, {non_user}",
		)
		self.assertEqual(notify.resolve_notify_recipients(step, doc), [u_on, non_user])

	def test_notify_mode_default_is_role(self):
		df = frappe.get_meta("Automation Step").get_field("notify_mode")
		self.assertIsNotNone(df)
		self.assertEqual(df.default, NOTIFY_MODE_ROLE)

	@patch("frappe.publish_realtime")
	@patch("frappe.enqueue")
	@patch(PUSH_PATH, return_value=["PNL-RETRY-1"])
	def test_retry_skips_notify_node_already_sent(self, mock_push, _mock_enqueue, _mock_realtime):
		frappe.set_user("Administrator")
		u1 = _ensure_user("retry_doc_event", enabled=1)
		todo = _make_todo(owner=u1)
		auto_name = _make_automation(
			[
				dict(_step(step_id="n1", notify_users=json.dumps([u1]), notify_title="ZZ notify retry {{ doc.name }}")),
				{"step_id": "u1", "step_type": UPDATE_FIELD, "update_field": "status", "update_value": "Closed"},
			],
			[("t1", "", "n1"), ("n1", "", "u1")],
			send_frequency="Every time",
		)

		with patch.object(steps, "_save_field", side_effect=frappe.ValidationError("bad value")):
			run1 = executor.run_automation(auto_name, "t1", "ToDo", todo.name)

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run1, "status"), RUN_STATUS_FAILED)
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u1, "document_type": "ToDo", "document_name": todo.name}), 1)
		self.assertEqual(mock_push.call_count, 1)

		res = api.retry_run(run1)
		executor.deliver_outbox(res["name"])

		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u1, "document_type": "ToDo", "document_name": todo.name}), 1)
		self.assertEqual(mock_push.call_count, 1)

		runs = frappe.get_all(RUN_DOCTYPE, filters={"automation": auto_name, "name": ("!=", run1)}, fields=["name", "status", "log"])
		self.assertEqual(len(runs), 1)
		self.assertEqual(runs[0].status, RUN_STATUS_COMPLETED)
		log2 = json.loads(runs[0].log)
		n1_entry = next(e for e in log2 if e["step_id"] == "n1")
		self.assertEqual(n1_entry["result"], RESULT_SKIPPED_ALREADY_SENT)
		self.assertEqual(n1_entry.get(STEP_OUTCOME_KEY), STEP_STATUS_SKIPPED)
		self.assertEqual(n1_entry.get("reason"), SKIP_REASON_ALREADY_SENT)

	@patch("frappe.publish_realtime")
	@patch("frappe.enqueue")
	@patch(PUSH_PATH, return_value=["PNL-RETRY-ERR"])
	def test_retry_reattempts_notify_that_errored(self, mock_push, _mock_enqueue, _mock_realtime):
		frappe.set_user("Administrator")
		u1 = _ensure_user("retry_notify_err", enabled=1)
		todo = _make_todo(owner=u1)
		auto_name = _make_automation(
			[dict(_step(step_id="n1", notify_users=json.dumps([u1]), notify_title="ZZ notify err {{ doc.name }}"))],
			[("t1", "", "n1")],
		)

		with patch.object(notify, "render_notify", side_effect=frappe.ValidationError("bad template")):
			run1 = executor.run_automation(auto_name, "t1", "ToDo", todo.name)

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run1, "status"), RUN_STATUS_FAILED)
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u1, "document_type": "ToDo", "document_name": todo.name}), 0)
		mock_push.assert_not_called()

		res = api.retry_run(run1)
		executor.deliver_outbox(res["name"])

		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u1, "document_type": "ToDo", "document_name": todo.name}), 1)
		self.assertEqual(mock_push.call_count, 1)

		runs = frappe.get_all(RUN_DOCTYPE, filters={"automation": auto_name, "name": ("!=", run1)}, fields=["name", "status", "log"])
		self.assertEqual(len(runs), 1)
		self.assertEqual(runs[0].status, RUN_STATUS_COMPLETED)
		log2 = json.loads(runs[0].log)
		n1_entry = next(e for e in log2 if e["step_id"] == "n1")
		self.assertEqual(n1_entry["result"], RESULT_SENT)
		self.assertEqual(n1_entry.get(STEP_OUTCOME_KEY), STEP_STATUS_SENT)

	@patch("frappe.publish_realtime")
	@patch("frappe.enqueue")
	@patch(PUSH_PATH, return_value=["PNL-GARBLED"])
	def test_retry_with_garbled_log_does_not_crash(self, mock_push, _mock_enqueue, _mock_realtime):
		frappe.set_user("Administrator")
		u1 = _ensure_user("retry_garbled", enabled=1)
		todo = _make_todo(owner=u1)
		auto_name = _make_automation(
			[dict(_step(step_id="n1", notify_users=json.dumps([u1]), notify_title="ZZ notify garbled {{ doc.name }}"))],
			[("t1", "", "n1")],
		)

		for bad_log in ("{not valid json", "", "{}", json.dumps([123, None, "bad"])):
			failed_run = frappe.get_doc({
				"doctype": RUN_DOCTYPE,
				"automation": auto_name,
				"trigger_id": "t1",
				"reference_doctype": "ToDo",
				"reference_name": todo.name,
				"status": RUN_STATUS_FAILED,
				"context": json.dumps({"value": ""}),
				"log": bad_log,
			}).insert(ignore_permissions=True, ignore_links=True)
			self.assertEqual(executor._sent_notification_nodes(failed_run.name), frozenset())

		res = api.retry_run(failed_run.name)
		executor.deliver_outbox(res["name"])

		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u1, "document_type": "ToDo", "document_name": todo.name}), 1)
		self.assertEqual(mock_push.call_count, 1)

	def test_retry_of_missing_run_is_empty_set(self):
		self.assertEqual(executor._sent_notification_nodes(None), frozenset())
		self.assertEqual(executor._sent_notification_nodes(""), frozenset())
		self.assertEqual(executor._sent_notification_nodes("RUN-DOES-NOT-EXIST-ZZ"), frozenset())

	@patch("frappe.publish_realtime")
	@patch("frappe.enqueue")
	@patch(PUSH_PATH, return_value=["PNL-TWO"])
	def test_two_notify_nodes_only_the_sent_one_is_skipped(self, mock_push, _mock_enqueue, _mock_realtime):
		frappe.set_user("Administrator")
		u1 = _ensure_user("two_nodes_1", enabled=1)
		u2 = _ensure_user("two_nodes_2", enabled=1)
		todo = _make_todo(owner=u1)
		auto_name = _make_automation(
			[
				dict(_step(step_id="n1", notify_users=json.dumps([u1]), notify_title="ZZ notify node1 {{ doc.name }}")),
				dict(_step(step_id="n2", notify_users=json.dumps([u2]), notify_title="ZZ notify node2 {{ doc.name }}")),
			],
			[("t1", "", "n1"), ("n1", "", "n2")],
		)

		real_render = notify.render_notify

		def fail_n2(step, doc):
			if step.step_id == "n2":
				raise frappe.ValidationError("n2 template error")
			return real_render(step, doc)

		with patch.object(notify, "render_notify", side_effect=fail_n2):
			run1 = executor.run_automation(auto_name, "t1", "ToDo", todo.name)

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run1, "status"), RUN_STATUS_FAILED)
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u1, "document_type": "ToDo", "document_name": todo.name}), 1)
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u2, "document_type": "ToDo", "document_name": todo.name}), 0)
		self.assertEqual(mock_push.call_count, 1)

		res = api.retry_run(run1)
		executor.deliver_outbox(res["name"])

		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u1, "document_type": "ToDo", "document_name": todo.name}), 1)
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u2, "document_type": "ToDo", "document_name": todo.name}), 1)
		self.assertEqual(mock_push.call_count, 2)
		self.assertEqual([c.kwargs["recipient"] for c in mock_push.call_args_list], [u1, u2])

		runs = frappe.get_all(RUN_DOCTYPE, filters={"automation": auto_name, "name": ("!=", run1)}, fields=["name", "status", "log"])
		self.assertEqual(len(runs), 1)
		self.assertEqual(runs[0].status, RUN_STATUS_COMPLETED)
		log2 = json.loads(runs[0].log)
		n1_entry = next(e for e in log2 if e["step_id"] == "n1")
		n2_entry = next(e for e in log2 if e["step_id"] == "n2")
		self.assertEqual(n1_entry["result"], RESULT_SKIPPED_ALREADY_SENT)
		self.assertEqual(n2_entry["result"], RESULT_SENT)

	@patch("frappe.publish_realtime")
	@patch(PUSH_PATH, return_value=["PNL-NONRETRY"])
	def test_non_retry_run_unaffected(self, mock_push, _mock_realtime):
		u1 = _ensure_user("non_retry", enabled=1)
		todo = _make_todo(owner=u1)
		auto_name = _make_automation(
			[dict(_step(step_id="n1", notify_users=json.dumps([u1]), notify_title="ZZ notify nonretry {{ doc.name }}"))],
			[("t1", "", "n1")],
			send_frequency="Every time",
		)

		run1 = executor.run_automation(auto_name, "t1", "ToDo", todo.name)
		run2 = executor.run_automation(auto_name, "t1", "ToDo", todo.name)

		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run1, "status"), RUN_STATUS_COMPLETED)
		self.assertEqual(frappe.db.get_value(RUN_DOCTYPE, run2, "status"), RUN_STATUS_COMPLETED)
		self.assertEqual(frappe.db.count("Notification Log", {"for_user": u1, "document_type": "ToDo", "document_name": todo.name}), 2)
		self.assertEqual(mock_push.call_count, 2)
