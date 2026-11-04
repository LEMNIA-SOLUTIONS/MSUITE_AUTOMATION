import http.client
import json
import unittest
from unittest.mock import MagicMock, patch

import frappe
import requests
import urllib3.exceptions

from msuite_inbox.inbox.constants import ChannelType
from msuite_inbox.inbox.services import outbound
from msuite_whatsapp.services.channels import inbox_channel as wa_channel
from msuite_automation.msuite_automation.engine import channels, executor
from msuite_whatsapp.integrations.meta.client import MetaAPIError, retry_kind

CH = "msuite_automation.msuite_automation.engine.channels"


class TestRetryKind(unittest.TestCase):
	def test_status_codes(self):
		for code in (429, 500, 503):
			err = MetaAPIError(f"HTTP {code}", status_code=code)
			self.assertEqual(retry_kind(err), "transient", f"status_code {code} should be transient")

		for code in (400, 401, 403, 404, 502, 504):
			err = MetaAPIError(f"HTTP {code}", status_code=code)
			self.assertIsNone(retry_kind(err), f"status_code {code} should not be retryable")

	def test_meta_error_codes(self):
		for code in (4, 80007, 130429, 131056, 133016):
			err = MetaAPIError("Meta rate limit", response_json={"error": {"code": code}}, status_code=400)
			self.assertEqual(retry_kind(err), "transient", f"Meta code {code} should be transient")

		for subcode in (4, 80007, 130429, 131056, 133016):
			err = MetaAPIError("Meta subcode", response_json={"error": {"code": 100, "error_subcode": subcode}}, status_code=400)
			self.assertEqual(retry_kind(err), "transient", f"Meta error_subcode {subcode} should be transient")

		for code in (100, 190, 131026):
			err = MetaAPIError("Meta terminal error", response_json={"error": {"code": code}}, status_code=400)
			self.assertIsNone(retry_kind(err), f"Meta code {code} should not be retryable")

	def test_network_connection_errors(self):
		bare_conn = requests.ConnectionError("x")
		self.assertIsNone(retry_kind(bare_conn))
		err_bare = MetaAPIError("Network connection failure")
		err_bare.__cause__ = bare_conn
		self.assertIsNone(retry_kind(err_bare))

		proto_err = requests.ConnectionError(
			urllib3.exceptions.ProtocolError("Connection aborted.", http.client.RemoteDisconnected("x"))
		)
		self.assertIsNone(retry_kind(proto_err))

		reset_err = requests.ConnectionError(ConnectionResetError("reset"))
		self.assertIsNone(retry_kind(reset_err))

		timeout_conn = requests.ConnectTimeout("Connect timeout")
		self.assertEqual(retry_kind(timeout_conn), "transient")
		err_timeout = MetaAPIError("Network connect timeout")
		err_timeout.__cause__ = timeout_conn
		self.assertEqual(retry_kind(err_timeout), "transient")

		max_retry = urllib3.exceptions.MaxRetryError(
			None, "u", reason=urllib3.exceptions.NewConnectionError(None, "refused")
		)
		conn_err = requests.ConnectionError(max_retry)
		self.assertEqual(retry_kind(conn_err), "transient")

		try:
			raise conn_err
		except requests.ConnectionError as e:
			try:
				raise MetaAPIError("Network connection failure") from e
			except MetaAPIError as meta_err:
				self.assertEqual(retry_kind(meta_err), "transient")

	def test_connection_error_with_context_only_returns_none(self):
		try:
			raise urllib3.exceptions.NewConnectionError(None, "earlier connection error")
		except urllib3.exceptions.NewConnectionError:
			try:
				raise requests.ConnectionError("handler failed")
			except requests.ConnectionError as exc:
				self.assertIsNotNone(exc.__context__)
				self.assertIsNone(exc.__cause__)
				self.assertIsNone(retry_kind(exc))

	def test_read_timeout_never_retries(self):
		read_err = requests.ReadTimeout("Read timed out")
		err = MetaAPIError("Network read timeout")
		err.__cause__ = read_err
		self.assertIsNone(retry_kind(err), "ReadTimeout on MetaAPIError must never retry")
		self.assertIsNone(retry_kind(read_err), "Direct ReadTimeout must never retry")

	def test_other_exceptions(self):
		self.assertIsNone(retry_kind(ValueError("bad value")))
		self.assertIsNone(retry_kind(RuntimeError("something failed")))
		self.assertIsNone(retry_kind(None))


class TestWhatsAppChannelSendRetry(unittest.TestCase):
	def _mock_get_doc(self, doctype, name=None):
		if doctype == "Channel Handle":
			return frappe._dict(name="CH-1", platform_handle="919876543210")
		if doctype == "Inbox":
			return frappe._dict(name="INBOX-1", whatsapp_account="ACC-1", channels=[])
		if doctype == "WhatsApp Account":
			return frappe._dict(name="ACC-1")
		if doctype == "Channel Message":
			mock_msg = MagicMock()
			mock_msg.save = MagicMock()
			return mock_msg
		return MagicMock()

	@patch("msuite_whatsapp.services.channels.inbox_channel.send_to_meta")
	@patch("msuite_whatsapp.services.channels.inbox_channel._record_outbound")
	@patch("msuite_whatsapp.services.channels.inbox_channel.frappe.get_doc")
	def test_channel_send_attaches_retry_on_transient_error(self, mock_get_doc, mock_record, mock_post):
		mock_get_doc.side_effect = self._mock_get_doc
		mock_record.return_value = {"message": "CM-1"}
		mock_post.side_effect = MetaAPIError("Service Unavailable", status_code=503)

		conv = frappe._dict(name="CONV-1", channel_handle="CH-1", inbox="INBOX-1")
		payload = {"type": "text", "body": "hello", "to": "919876543210"}
		with patch("msuite_whatsapp.services.channels.inbox_channel._build_send_body", return_value={"messaging_product": "whatsapp"}):
			res = wa_channel.send(conv, payload)

		self.assertIsInstance(res, dict)
		self.assertFalse(res["ok"])
		self.assertEqual(res.get("retry"), "transient")

	@patch("msuite_whatsapp.services.channels.inbox_channel.send_to_meta")
	@patch("msuite_whatsapp.services.channels.inbox_channel._record_outbound")
	@patch("msuite_whatsapp.services.channels.inbox_channel.frappe.get_doc")
	def test_channel_send_no_retry_on_terminal_error(self, mock_get_doc, mock_record, mock_post):
		mock_get_doc.side_effect = self._mock_get_doc
		mock_record.return_value = {"message": "CM-1"}
		mock_post.side_effect = MetaAPIError("Bad Request", status_code=400, response_json={"error": {"code": 100}})

		conv = frappe._dict(name="CONV-1", channel_handle="CH-1", inbox="INBOX-1")
		payload = {"type": "text", "body": "hello", "to": "919876543210"}
		with patch("msuite_whatsapp.services.channels.inbox_channel._build_send_body", return_value={"messaging_product": "whatsapp"}):
			res = wa_channel.send(conv, payload)

		self.assertIsInstance(res, dict)
		self.assertFalse(res["ok"])
		self.assertIsNone(res.get("retry"))


class TestOutboundSendRetryPropagation(unittest.TestCase):
	@patch("msuite_inbox.inbox.services.outbound.handler_for")
	@patch("msuite_inbox.inbox.services.outbound._upsert_outbound_conversation")
	@patch("msuite_inbox.inbox.services.outbound._upsert_handle")
	@patch("msuite_inbox.inbox.services.outbound.frappe.get_doc")
	def test_outbound_propagates_retry(self, mock_get_doc, mock_handle, mock_conv, mock_handler_for):
		mock_get_doc.return_value = frappe._dict(name="INBOX-1", channel_type=ChannelType.WHATSAPP)
		mock_handle.return_value = frappe._dict(name="H-1")
		mock_conv.return_value = frappe._dict(name="CONV-1", inbox="INBOX-1")

		mock_handler = MagicMock()
		mock_handler.send.return_value = {"message": "CM-1", "ok": False, "error": "Rate limit", "retry": "transient"}
		mock_handler_for.return_value = mock_handler

		res = outbound.send_message(ChannelType.WHATSAPP, "919876543210", {"text": "hi"}, inbox="INBOX-1")

		self.assertFalse(res["ok"])
		self.assertEqual(res.get("retry"), "transient")


class TestSendWhatsAppRetry(unittest.TestCase):
	@patch(f"{CH}.send_to_phone")
	@patch(f"{CH}._template")
	def test_send_whatsapp_raises_send_retry_on_transient(self, tpl, send):
		tpl.return_value = frappe._dict(name="t", language_code="en", parameter_format="POSITIONAL", template="Hi", header_type=None, buttons=[])
		send.return_value = {"ok": False, "error": "Cloud API rate limit", "retry": "transient"}

		a = frappe._dict(name="MA-1", whatsapp_account="acc", phone_field=None, variable_mappings=[])
		step = frappe._dict(step_id="wa", template="t", header_source="None", button_fields=None)
		doc = frappe._dict(doctype="CRM Lead", name="L1", mobile_no="9876543210")

		with self.assertRaises(channels.SendRetry) as ctx:
			channels.send_whatsapp(a, step, doc)

		self.assertEqual(ctx.exception.kind, "transient")
		self.assertIn("Cloud API rate limit", str(ctx.exception))
		self.assertIsInstance(ctx.exception, RuntimeError)

	@patch(f"{CH}.send_to_phone")
	@patch(f"{CH}._template")
	def test_send_whatsapp_raises_plain_runtime_error_when_no_retry(self, tpl, send):
		tpl.return_value = frappe._dict(name="t", language_code="en", parameter_format="POSITIONAL", template="Hi", header_type=None, buttons=[])
		send.return_value = {"ok": False, "error": "Invalid recipient number"}

		a = frappe._dict(name="MA-1", whatsapp_account="acc", phone_field=None, variable_mappings=[])
		step = frappe._dict(step_id="wa", template="t", header_source="None", button_fields=None)
		doc = frappe._dict(doctype="CRM Lead", name="L1", mobile_no="9876543210")

		with self.assertRaises(RuntimeError) as ctx:
			channels.send_whatsapp(a, step, doc)

		self.assertNotIsInstance(ctx.exception, channels.SendRetry)
		self.assertIn("Invalid recipient number", str(ctx.exception))


TITLE = "ZZ send retry test"


def make_auto(steps, edges, triggers=None, disabled=0, **kw):
	doc = frappe.get_doc({
		"doctype": "Messaging Automation",
		"title": TITLE,
		"disabled": 1,
		"triggers": triggers or [{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
		"steps": steps,
		"edges": [{"source_id": a, "source_handle": h, "target_id": b} for a, h, b in edges],
		**kw,
	})
	doc.flags.ignore_plan_gate = True
	doc.insert(ignore_permissions=True, ignore_links=True)
	if not disabled:
		frappe.db.set_value("Messaging Automation", doc.name, "disabled", 0)
	return doc.name


def make_todo(desc="ZZ send retry test", **kw):
	return frappe.get_doc({"doctype": "ToDo", "description": desc, **kw}).insert(ignore_permissions=True).name


def wa_step(sid, template="t1"):
	return {"step_id": sid, "step_type": "Send WhatsApp", "template": template}


class TestSendParkAndRetry(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": TITLE}, pluck="name"):
			frappe.db.delete("Messaging Automation Run", {"automation": name})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "ZZ send retry%")})

	@patch(f"{CH}.send_whatsapp")
	def test_second_send_retry_count_starts_from_zero(self, mock_send):
		mock_send.side_effect = [channels.SendRetry("transient", "503"), True, channels.SendRetry("transient", "503")]
		name = make_auto([wa_step("w1"), wa_step("w2")], [("t1", "", "w1"), ("w1", "", "w2")])
		run = executor.run_automation(name, "t1", "ToDo", make_todo())
		self.assertEqual(frappe.db.get_value("Automation Run Path", {"parent": run}, "retry_count"), 1)

		from frappe.utils import add_to_date, now_datetime
		past = add_to_date(now_datetime(), minutes=-1)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()

		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["node_id", "status", "retry_count"])
		self.assertEqual([(p.node_id, p.status, p.retry_count) for p in paths], [("w2", "Waiting", 1)])

	@patch(f"{CH}.send_whatsapp")
	def test_first_attempt_send_retry_transient(self, mock_send):
		mock_send.side_effect = channels.SendRetry("transient", "Meta 503 Service Unavailable")
		name = make_auto([wa_step("w1")], [("t1", "", "w1")])
		ref = make_todo()
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertIsNotNone(run)

		# run Waiting (not Failed)
		run_status = frappe.db.get_value("Messaging Automation Run", run, "status")
		self.assertEqual(run_status, "Waiting")

		# path Waiting, retry_count 1, resume_at about now+60s
		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["status", "retry_count", "resume_at"])
		self.assertEqual(len(paths), 1)
		path = paths[0]
		self.assertEqual(path.status, "Waiting")
		self.assertEqual(path.retry_count, 1)

		# resume_at about now + 60s
		from frappe.utils import add_to_date, now_datetime
		expected_resume = add_to_date(now_datetime(), seconds=60)
		diff_seconds = abs((path.resume_at - expected_resume).total_seconds())
		self.assertLess(diff_seconds, 15)

		# log result starts 'retrying (1/3)'
		import json
		log = json.loads(frappe.db.get_value("Messaging Automation Run", run, "log"))
		send_entry = next(e for e in log if e.get("step_id") == "w1")
		self.assertTrue(send_entry["result"].startswith("retrying (1/3)"))

	@patch(f"{CH}.send_whatsapp")
	def test_resume_reexecutes_and_succeeds(self, mock_send):
		mock_send.side_effect = [channels.SendRetry("transient", "Meta 503"), True]
		name = make_auto([wa_step("w1")], [("t1", "", "w1")])
		ref = make_todo()
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Waiting")
		self.assertEqual(mock_send.call_count, 1)

		from frappe.utils import add_to_date, now_datetime
		past = add_to_date(now_datetime(), minutes=-1)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))

		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()

		self.assertEqual(mock_send.call_count, 2)
		run_doc = frappe.get_doc("Messaging Automation Run", run)
		self.assertEqual(run_doc.status, "Completed")
		self.assertEqual(run_doc.sent_count, 1)
		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["retry_count", "status"])
		self.assertEqual(paths[0].retry_count, 0)

	@patch(f"{CH}.send_whatsapp")
	def test_retries_exhausted_fails(self, mock_send):
		mock_send.side_effect = channels.SendRetry("transient", "rate limit")
		name = make_auto([wa_step("w1")], [("t1", "", "w1")])
		ref = make_todo()
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Waiting")
		self.assertEqual(mock_send.call_count, 1)

		from frappe.utils import add_to_date, now_datetime
		past = add_to_date(now_datetime(), minutes=-1)

		# Retry 1 (2nd attempt): fails -> retry_count 2, retrying (2/3)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Waiting")
		self.assertEqual(mock_send.call_count, 2)
		self.assertEqual(frappe.db.get_value("Automation Run Path", {"parent": run}, "retry_count"), 2)

		# Retry 2 (3rd attempt): fails -> retry_count 3, retrying (3/3)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Waiting")
		self.assertEqual(mock_send.call_count, 3)
		self.assertEqual(frappe.db.get_value("Automation Run Path", {"parent": run}, "retry_count"), 3)

		# Retry 3 (4th attempt): fails -> exhausted -> log 'error', run Failed
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(mock_send.call_count, 4)
		run_doc = frappe.get_doc("Messaging Automation Run", run)
		self.assertEqual(run_doc.status, "Failed")
		self.assertEqual(run_doc.sent_count, 0)
		import json
		log = json.loads(run_doc.log)
		w1_entries = [e for e in log if e.get("step_id") == "w1"]
		self.assertEqual(len(w1_entries), 4)
		self.assertEqual(w1_entries[-1]["result"], "error")

	@patch(f"{CH}.send_whatsapp")
	def test_non_retryable_runtime_error(self, mock_send):
		mock_send.side_effect = RuntimeError("Invalid recipient phone number")
		name = make_auto([wa_step("w1")], [("t1", "", "w1")])
		ref = make_todo()
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertEqual(mock_send.call_count, 1)
		run_doc = frappe.get_doc("Messaging Automation Run", run)
		self.assertEqual(run_doc.status, "Failed")
		import json
		log = json.loads(run_doc.log)
		w1_entries = [e for e in log if e.get("step_id") == "w1"]
		self.assertEqual(len(w1_entries), 1)
		self.assertEqual(w1_entries[0]["result"], "error")

	@patch(f"{CH}.send_whatsapp")
	def test_retry_then_success_then_normal_wait_continues(self, mock_send):
		mock_send.side_effect = [channels.SendRetry("transient", "fail"), True, True]
		name = make_auto(
			[wa_step("w1"), {"step_id": "wait1", "step_type": "Wait", "wait_value": 1, "wait_unit": "Days"}, wa_step("w2")],
			[("t1", "", "w1"), ("w1", "", "wait1"), ("wait1", "", "w2")]
		)
		ref = make_todo()
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Waiting")
		self.assertEqual(mock_send.call_count, 1)

		from frappe.utils import add_to_date, now_datetime
		past = add_to_date(now_datetime(), minutes=-1)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()

		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Waiting")
		self.assertEqual(mock_send.call_count, 2)
		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["node_id", "status", "retry_count", "executed"])
		self.assertEqual(len(paths), 1)
		self.assertEqual(paths[0].node_id, "wait1")
		self.assertEqual(paths[0].status, "Waiting")
		self.assertEqual(paths[0].retry_count, 0)
		self.assertEqual(paths[0].executed, 1)

		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()

		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Completed")
		self.assertEqual(mock_send.call_count, 3)
		import json
		log_steps = [e["step_id"] for e in json.loads(frappe.db.get_value("Messaging Automation Run", run, "log"))]
		self.assertEqual(log_steps, ["t1", "w1", "w1", "wait1", "w2"])

	@patch(f"{CH}.send_whatsapp")
	def test_retries_exhausted_then_normal_wait_continues(self, mock_send):
		mock_send.side_effect = [
			channels.SendRetry("transient", "fail 1"),
			channels.SendRetry("transient", "fail 2"),
			channels.SendRetry("transient", "fail 3"),
			channels.SendRetry("transient", "fail 4"),
			True,
		]
		name = make_auto(
			[wa_step("w1"), {"step_id": "wait1", "step_type": "Wait", "wait_value": 1, "wait_unit": "Days"}, wa_step("w2")],
			[("t1", "", "w1"), ("w1", "", "wait1"), ("wait1", "", "w2")]
		)
		ref = make_todo()
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Waiting")
		self.assertEqual(mock_send.call_count, 1)

		from frappe.utils import add_to_date, now_datetime
		past = add_to_date(now_datetime(), minutes=-1)

		# Retry 1 (2nd attempt): fails -> retry_count 2
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(mock_send.call_count, 2)
		self.assertEqual(frappe.db.get_value("Automation Run Path", {"parent": run}, "retry_count"), 2)

		# Retry 2 (3rd attempt): fails -> retry_count 3
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(mock_send.call_count, 3)
		self.assertEqual(frappe.db.get_value("Automation Run Path", {"parent": run}, "retry_count"), 3)

		# Retry 3 (4th attempt): fails -> exhausted. Walker proceeds to wait1.
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()
		self.assertEqual(mock_send.call_count, 4)

		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["node_id", "status", "retry_count", "executed"])
		self.assertEqual(len(paths), 1)
		self.assertEqual(paths[0].node_id, "wait1")
		self.assertEqual(paths[0].status, "Waiting")
		self.assertEqual(paths[0].retry_count, 0)
		self.assertEqual(paths[0].executed, 1)

		# On resume past the Wait, the Wait must continue (not re-execute)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()

		self.assertEqual(mock_send.call_count, 5)
		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["node_id", "status", "retry_count", "executed"])
		self.assertEqual(len(paths), 1)
		self.assertEqual(paths[0].node_id, "w2")
		import json
		log_steps = [e["step_id"] for e in json.loads(frappe.db.get_value("Messaging Automation Run", run, "log"))]
		self.assertEqual(log_steps, ["t1", "w1", "w1", "w1", "w1", "wait1", "w2"])

	@patch(f"{CH}.send_whatsapp")
	def test_retry_then_runtime_error_then_wait_has_zero_retry_count(self, mock_send):
		mock_send.side_effect = [channels.SendRetry("transient", "fail"), RuntimeError("plain error"), True]
		name = make_auto(
			[wa_step("w1"), {"step_id": "wait1", "step_type": "Wait", "wait_value": 1, "wait_unit": "Days"}, wa_step("w2")],
			[("t1", "", "w1"), ("w1", "", "wait1"), ("wait1", "", "w2")]
		)
		ref = make_todo()
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Waiting")
		self.assertEqual(mock_send.call_count, 1)

		from frappe.utils import add_to_date, now_datetime
		past = add_to_date(now_datetime(), minutes=-1)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()

		self.assertEqual(mock_send.call_count, 2)
		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["node_id", "status", "retry_count", "executed"])
		self.assertEqual(len(paths), 1)
		self.assertEqual(paths[0].node_id, "wait1")
		self.assertEqual(paths[0].status, "Waiting")
		self.assertEqual(paths[0].retry_count, 0)
		self.assertEqual(paths[0].executed, 1)

		# Verify wait continues on next resume rather than re-executing
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))
		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()

		self.assertEqual(mock_send.call_count, 3)
		import json
		log_steps = [e["step_id"] for e in json.loads(frappe.db.get_value("Messaging Automation Run", run, "log"))]
		self.assertEqual(log_steps, ["t1", "w1", "w1", "wait1", "w2"])

	@patch(f"{CH}.send_whatsapp")
	def test_once_per_day_retry_sends_but_later_fresh_run_skipped(self, mock_send):
		from msuite_automation.msuite_automation.constants import ONCE_PER_DAY

		name = make_auto([wa_step("w1", template="tpl_daily")], [("t1", "", "w1")], send_frequency=ONCE_PER_DAY)
		ref = make_todo("ZZ send retry daily e2e")

		def first_send(*args, **kwargs):
			cm = frappe.get_doc({
				"doctype": "Channel Message",
				"channel_type": "WhatsApp",
				"direction": "Outbound",
				"status": "failed",
				"reference_doctype": "ToDo",
				"reference_name": ref,
				"template_name": "tpl_daily",
			})
			cm.flags.ignore_mandatory = True
			cm.insert(ignore_permissions=True, ignore_links=True)
			raise channels.SendRetry("transient", "Meta 503 Service Unavailable")

		mock_send.side_effect = first_send

		# First run: attempt fails with SendRetry transient, writes 'failed' Channel Message
		run = executor.run_automation(name, "t1", "ToDo", ref)
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Waiting")
		self.assertEqual(mock_send.call_count, 1)

		# The retry: resume_waiting_runs with due retry path
		mock_send.side_effect = [True]
		from frappe.utils import add_to_date, now_datetime
		past = add_to_date(now_datetime(), minutes=-1)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))

		with patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()

		# Retry sends (not skipped!)
		self.assertEqual(mock_send.call_count, 2)
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Completed")

		# Fresh run on the same doc later the same day after a final 'failed' message is skipped as already sent
		ref2 = make_todo("ZZ send retry daily fresh run")
		cm2 = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "WhatsApp",
			"direction": "Outbound",
			"status": "failed",
			"reference_doctype": "ToDo",
			"reference_name": ref2,
			"template_name": "tpl_daily",
		})
		cm2.flags.ignore_mandatory = True
		cm2.insert(ignore_permissions=True, ignore_links=True)

		prev_run = frappe.get_doc({
			"doctype": "Messaging Automation Run",
			"automation": name,
			"status": "Failed",
			"reference_doctype": "ToDo",
			"reference_name": ref2,
			"trigger_id": "t1",
		})
		prev_run.flags.ignore_mandatory = True
		prev_run.insert(ignore_permissions=True, ignore_links=True)

		mock_send.reset_mock()
		fresh_run = executor.run_automation(name, "t1", "ToDo", ref2)
		self.assertEqual(mock_send.call_count, 0)
		fresh_log = json.loads(frappe.db.get_value("Messaging Automation Run", fresh_run, "log"))
		w1_entry = next(e for e in fresh_log if e.get("step_id") == "w1")
		self.assertEqual(w1_entry["result"], "skipped: already sent")


class TestAlreadySentStatusFilter(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		frappe.db.delete("ToDo", {"description": ("like", "ZZ send retry%")})

	def test_already_sent_once_per_day_failed_message(self):
		from msuite_automation.msuite_automation.constants import ONCE_PER_DAY, ONCE_PER_DOCUMENT

		todo_id = make_todo("ZZ send retry already sent")
		cm = frappe.get_doc({
			"doctype": "Channel Message",
			"channel_type": "WhatsApp",
			"direction": "Outbound",
			"status": "failed",
			"reference_doctype": "ToDo",
			"reference_name": todo_id,
			"template_name": "test_tpl",
		})
		cm.flags.ignore_mandatory = True
		cm.insert(ignore_permissions=True, ignore_links=True)

		# ONCE_PER_DAY with a failed message today -> already_sent True normally and False with ignore_failed=True
		self.assertTrue(channels.already_sent("test_tpl", "ToDo", todo_id, ONCE_PER_DAY))
		self.assertFalse(channels.already_sent("test_tpl", "ToDo", todo_id, ONCE_PER_DAY, ignore_failed=True))
		# ONCE_PER_DOCUMENT keeps the status filter as before
		self.assertFalse(channels.already_sent("test_tpl", "ToDo", todo_id, ONCE_PER_DOCUMENT))

		frappe.db.set_value("Channel Message", cm.name, "status", "sent")
		self.assertTrue(channels.already_sent("test_tpl", "ToDo", todo_id, ONCE_PER_DAY))
		self.assertTrue(channels.already_sent("test_tpl", "ToDo", todo_id, ONCE_PER_DAY, ignore_failed=True))
		self.assertTrue(channels.already_sent("test_tpl", "ToDo", todo_id, ONCE_PER_DOCUMENT))


class TestPersistLastSends(unittest.TestCase):
	def test_persist_keys_by_path_id_and_node_id(self):
		run = frappe._dict(paths=[], status=None, log=None, sent_count=0, flags=frappe._dict(), save=MagicMock())
		run.set = lambda k, v: setattr(run, k, v)
		paths = [
			{"path_id": "p1", "node_id": "w1", "status": "Completed", "executed": 1},
			{"path_id": "p2", "node_id": "w1", "status": "Completed", "executed": 1},
		]
		# If keyed only by node_id, p2's error would overwrite p1's success, causing failed=True -> status="Failed".
		# When keyed by (path_id, node_id), both are tracked, so failed=False -> status="Completed".
		log = [
			{"path_id": "p1", "node_id": "w1", "step_type": "Send WhatsApp", "result": "sent"},
			{"path_id": "p2", "node_id": "w1", "step_type": "Send WhatsApp", "result": "error"},
		]
		executor._persist(run, paths, log)
		self.assertEqual(run.status, "Completed")


class TestSendDedupeLockUnavailable(unittest.TestCase):
	def setUp(self):
		self._cleanup()

	def tearDown(self):
		self._cleanup()

	def _cleanup(self):
		frappe.db.rollback()
		for name in frappe.get_all("Messaging Automation", filters={"title": TITLE}, pluck="name"):
			frappe.db.delete("Messaging Automation Run", {"automation": name})
			frappe.delete_doc("Messaging Automation", name, force=True, ignore_permissions=True)
		frappe.db.delete("ToDo", {"description": ("like", "ZZ send retry%")})

	def test_send_lock_unavailable_raises_transient_retry(self):
		from msuite_automation.msuite_automation.engine import steps as S

		ctx = frappe._dict(
			automation=frappe._dict(name="MA-1", send_frequency="Every time"),
			value="val-1",
			discriminator="disc",
			doc=frappe._dict(doctype="ToDo", name="T1"),
		)
		step = frappe._dict(step_id="w1")
		fn = MagicMock()

		with patch(f"{CH}.acquire_lock", side_effect=channels.LockUnavailable("redis down")):
			with self.assertRaises(channels.SendRetry) as exc_info:
				S._send(ctx, step, "WhatsApp", fn)

		self.assertEqual(exc_info.exception.kind, "transient")
		self.assertIn("duplicate-send guard unavailable", str(exc_info.exception))
		fn.assert_not_called()

	def test_send_lock_already_held_returns_duplicate(self):
		from msuite_automation.msuite_automation.engine import steps as S

		ctx = frappe._dict(
			automation=frappe._dict(name="MA-1", send_frequency="Every time"),
			value="val-1",
			discriminator="disc",
			doc=frappe._dict(doctype="ToDo", name="T1"),
		)
		step = frappe._dict(step_id="w1")
		fn = MagicMock()

		with patch(f"{CH}.acquire_lock", return_value=False):
			res = S._send(ctx, step, "WhatsApp", fn)

		self.assertEqual(res, "skipped: duplicate")
		fn.assert_not_called()

	@patch(f"{CH}.send_whatsapp")
	def test_walker_retries_on_lock_unavailable(self, mock_send):
		name = make_auto([wa_step("w1")], [("t1", "", "w1")], send_frequency="Every time")
		ref = make_todo()
		mock_send.return_value = True

		# First run: acquire_lock raises LockUnavailable
		with patch(f"{CH}.acquire_lock", side_effect=channels.LockUnavailable("redis down")):
			run = executor.run_automation(name, "t1", "ToDo", ref, value="val1")

		self.assertIsNotNone(run)
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Waiting")
		paths = frappe.get_all("Automation Run Path", filters={"parent": run}, fields=["status", "retry_count"])
		self.assertEqual(len(paths), 1)
		self.assertEqual(paths[0].status, "Waiting")
		self.assertEqual(paths[0].retry_count, 1)
		mock_send.assert_not_called()

		# Next resume: acquire_lock succeeds (returns True), message sends once
		from frappe.utils import add_to_date, now_datetime
		past = add_to_date(now_datetime(), minutes=-1)
		frappe.db.sql("update `tabAutomation Run Path` set resume_at=%s where parent=%s", (past, run))
		frappe.db.sql("update `tabMessaging Automation Run` set resume_at=%s where name=%s", (past, run))

		with patch(f"{CH}.acquire_lock", return_value=True), patch("frappe.db.commit"), patch.object(executor, "_due_run_names", return_value=[run]):
			executor.resume_waiting_runs()

		self.assertEqual(mock_send.call_count, 1)
		self.assertEqual(frappe.db.get_value("Messaging Automation Run", run, "status"), "Completed")
