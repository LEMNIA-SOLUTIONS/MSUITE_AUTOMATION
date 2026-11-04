import json
import unittest
from unittest.mock import patch

import frappe

from msuite_automation.msuite_automation.api import automations as api


class TestAutomationsApi(unittest.TestCase):
	def setUp(self):
		frappe.set_user("Administrator")

	def tearDown(self):
		frappe.db.rollback()

	def _payload(self, **kw):
		return {"title": "ZZ api", "disabled": 1,
			"triggers": [{"trigger_id": "t1", "trigger_type": "Field Change", "reference_doctype": "ToDo", "field": "status", "to_value": "Closed"}],
			"steps": [{"step_id": "s1", "step_type": "Send SMS", "sms_message": "hi"}],
			"edges": [{"source_id": "t1", "source_handle": "", "target_id": "s1"}], **kw}

	def test_save_and_get_round_trip(self):
		name = api.save_automation(self._payload())["name"]
		got = api.get_automation(name)
		self.assertEqual([t["to_value"] for t in got["triggers"]], ["Closed"])
		self.assertEqual([s["step_id"] for s in got["steps"]], ["s1"])
		got["steps"].append({"step_id": "s2", "step_type": "Send Email", "email_subject": "x"})
		api.save_automation(got)
		self.assertEqual(len(api.get_automation(name)["steps"]), 2)

	def test_graph_tables_round_trip(self):
		name = api.save_automation(self._payload(recipients=[{"reference_doctype": "ToDo", "phone_field": "description"}],
			notes=[{"note_id": "n1", "text": "remember", "pos_x": 10, "pos_y": 20}]))["name"]
		got = api.get_automation(name)
		self.assertEqual([(e["source_id"], e["target_id"]) for e in got["edges"]], [("t1", "s1")])
		self.assertEqual(got["recipients"][0]["phone_field"], "description")
		self.assertEqual(got["notes"][0]["text"], "remember")

	def test_save_twice_does_not_duplicate(self):
		p = self._payload()
		name = api.save_automation(p)["name"]
		p["name"] = name
		api.save_automation(p)
		self.assertEqual(frappe.db.count("Messaging Automation", {"title": "ZZ api"}), 1)

	def test_toggle_validates_activation(self):
		name = api.save_automation(self._payload(steps=[], edges=[]))["name"]
		with self.assertRaises(frappe.ValidationError):
			api.toggle_automation(name, 1)

	def test_list_summary_and_channels(self):
		name = api.save_automation(self._payload())["name"]
		row = next(r for r in api.get_automations(search="ZZ api")["items"] if r["name"] == name)
		self.assertEqual(row["when_summary"], "ToDo: Status → Closed")
		self.assertEqual(row["channels"], ["SMS"])

	def test_summary_from_trigger_rows(self):
		name = api.save_automation(self._payload(triggers=[
			{"trigger_id": "t1", "trigger_type": "Field Change", "reference_doctype": "ToDo", "field": "status", "to_value": "Closed"},
			{"trigger_id": "t2", "trigger_type": "Document Event", "reference_doctype": "Note", "doc_event": "After Save"}],
			edges=[{"source_id": "t1", "target_id": "s1"}, {"source_id": "t2", "target_id": "s1"}]))["name"]
		row = next(r for r in api.get_automations(search="ZZ api")["items"] if r["name"] == name)
		self.assertEqual(row["when_summary"], "ToDo: Status → Closed + 1 more")
		self.assertEqual(row["doctypes"], ["Note", "ToDo"])
		self.assertIn(name, [r["name"] for r in api.get_automations(reference_doctype="Note")["items"]])
		self.assertIn(name, [r["name"] for r in api.get_automations(trigger_type="Document Event")["items"]])

	def test_stats_by_trigger_rows(self):
		before = api.get_stats()
		api.save_automation(self._payload())
		after = api.get_stats()
		self.assertEqual(after["by_trigger"].get("Field Change", 0), before["by_trigger"].get("Field Change", 0) + 1)
		self.assertIn("ToDo", after["doctypes"])

	def test_list_summary_with_no_triggers(self):
		self.assertEqual(api.when_summary({"triggers": []}), "")

	def test_duplicate(self):
		name = api.save_automation(self._payload())["name"]
		copy = api.duplicate_automation(name)["name"]
		self.assertEqual(frappe.db.get_value("Messaging Automation", copy, ["title", "disabled"]), ("ZZ api (copy)", 1))

	def test_doctype_fields_delivery_note(self):
		fields = api.get_doctype_fields("Delivery Note")
		self.assertTrue(any(f["fieldname"] == "sales_order.status" for f in fields))
		self.assertFalse(any(f["fieldtype"] in ("Section Break", "Table") for f in fields))

	def test_get_ig_step_meta_matches_backend_constants(self):
		from msuite_core import constants as c
		from msuite_automation.msuite_automation import constants as automation_constants
		meta = api.get_ig_step_meta()
		self.assertEqual(meta["title_max"], c.MESSENGER_TEMPLATE_TITLE_MAX)
		self.assertEqual(meta["subtitle_max"], c.MESSENGER_TEMPLATE_SUBTITLE_MAX)
		self.assertEqual(meta["button_title_max"], c.MESSENGER_BUTTON_TITLE_MAX)
		self.assertEqual(meta["expiry_min"], automation_constants.IG_CONSENT_MIN_EXPIRY_DAYS)
		self.assertEqual(meta["expiry_default"], automation_constants.IG_CONSENT_DEFAULT_EXPIRY_DAYS)
		self.assertEqual(meta["image_source_post"], c.IG_CONSENT_IMAGE_SOURCE_POST)
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			api.get_ig_step_meta()

	def test_permission(self):
		frappe.set_user("Guest")
		with self.assertRaises(frappe.PermissionError):
			api.get_automations()

	def test_bulk_delete_with_missing_name(self):
		real = api.save_automation(self._payload())["name"]
		missing = "NONEXISTENT_AUTO_TEST_XYZ"
		res = api.bulk_delete_automations([real, missing])
		self.assertEqual(res["deleted"], [real])
		self.assertEqual(res["failed"], [{"name": missing, "error": "Not found"}])
		self.assertFalse(frappe.db.exists("Messaging Automation", real))
		with self.assertRaises(frappe.DoesNotExistError):
			api.delete_automation(missing)

	def _ensure_wa_manager(self):
		email = "test_wa_mgr_api@example.com"
		if not frappe.db.exists("User", email):
			u = frappe.new_doc("User")
			u.email = email
			u.first_name = "Test WA Mgr"
			u.flags.ignore_permissions = True
			u.insert()
			u.add_roles("WhatsApp Manager")
		return email

	def test_get_automation_applies_fieldlevel_read_permissions(self):
		p = self._payload()
		p["triggers"][0]["condition"] = "doc.status == 'Closed'"
		name = api.save_automation(p)["name"]
		with unittest.mock.patch.object(frappe.model.document.Document, "apply_fieldlevel_read_permissions") as mock_apply:
			api.get_automation(name)
			mock_apply.assert_called_once()

	def test_duplicate_preserves_condition_for_manager(self):
		mgr = self._ensure_wa_manager()
		frappe.set_user("Administrator")
		p = self._payload()
		p["triggers"][0]["condition"] = "doc.status == 'Closed'"
		name = api.save_automation(p)["name"]

		frappe.set_user(mgr)
		try:
			copy_name = api.duplicate_automation(name)["name"]
			frappe.set_user("Administrator")
			copy_doc = frappe.get_doc("Messaging Automation", copy_name)
			self.assertEqual(copy_doc.triggers[0].condition, "doc.status == 'Closed'")
		finally:
			frappe.set_user("Administrator")

	def test_save_ignores_condition_without_permission(self):
		mgr = self._ensure_wa_manager()
		frappe.set_user(mgr)
		try:
			p = self._payload()
			p["triggers"][0]["condition"] = "True"
			name = api.save_automation(p)["name"]
			frappe.set_user("Administrator")
			saved = frappe.get_doc("Messaging Automation", name)
			self.assertEqual(saved.triggers[0].condition, "")
		finally:
			frappe.set_user("Administrator")

	def test_save_keeps_saved_condition_without_permission(self):
		mgr = self._ensure_wa_manager()
		p = self._payload()
		p["triggers"][0]["condition"] = "doc.x"
		name = api.save_automation(p)["name"]
		frappe.set_user(mgr)
		try:
			got = api.get_automation(name)
			got["triggers"][0]["condition"] = ""
			api.save_automation(got)
			frappe.set_user("Administrator")
			saved = frappe.get_doc("Messaging Automation", name)
			self.assertEqual(saved.triggers[0].condition, "doc.x")
		finally:
			frappe.set_user("Administrator")

	def test_delete_automation_checks_delete_permission(self):
		p = self._payload()
		name = api.save_automation(p)["name"]
		with unittest.mock.patch("msuite_automation.msuite_automation.api.automations.check_delete") as mock_check_del:
			api.delete_automation(name)
			mock_check_del.assert_called_once_with("Messaging Automation")

		name2 = api.save_automation(p)["name"]
		with unittest.mock.patch("msuite_automation.msuite_automation.api.automations.check_delete") as mock_check_del:
			api.bulk_delete_automations([name2])
			mock_check_del.assert_called_once_with("Messaging Automation")

	def test_dispatched_counts_run_sends(self):
		before = api.get_stats()["dispatched"]
		name = api.save_automation(self._payload())["name"]
		for n in (2, 3):
			frappe.get_doc({"doctype": "Messaging Automation Run", "automation": name, "status": "Completed", "log": "[]",
				"sent_count": n}).insert(ignore_permissions=True, ignore_links=True)
		self.assertEqual(api.get_stats()["dispatched"], before + 5)

	def _todo(self):
		return frappe.get_doc({"doctype": "ToDo", "description": "ZZ test run"}).insert(ignore_permissions=True).name

	@patch("msuite_automation.msuite_automation.engine.channels.send_to_phone")
	def test_test_automation_dry_run(self, send):
		docname = self._todo()
		runs_before = frappe.db.count("Messaging Automation Run")
		out = api.test_automation(self._payload(), docname, "t1")
		self.assertIn("filters", out)
		self.assertEqual([s["step_id"] for s in out["steps"]], ["s1"])
		self.assertEqual(frappe.db.count("Messaging Automation Run"), runs_before)
		send.assert_not_called()

	def test_test_automation_missing_record(self):
		with self.assertRaises(frappe.DoesNotExistError):
			api.test_automation(self._payload(), "does-not-exist")

	def test_test_automation_checks_record_permission(self):
		docname = self._todo()
		frappe.set_user("Guest")
		try:
			with self.assertRaises(frappe.PermissionError):
				api.test_automation(self._payload(), docname)
		finally:
			frappe.set_user("Administrator")

	def test_test_automation_ignores_unsaved_condition_without_permission(self):
		docname = self._todo()
		with patch("frappe.model.meta.Meta.get_permlevel_access", return_value=[0]):
			out = api.test_automation(self._payload(triggers=[
				{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save", "condition": "False"}
			]), docname)
		self.assertTrue(out["filters"]["passed"])  # unsaved condition "False" ignored

	def test_get_runs_newest_first_and_bad_log(self):
		name = api.save_automation(self._payload())["name"]
		for i, log in enumerate(['[{"step_id": "s1", "step_type": "Send SMS", "result": "sent", "at": "x"}]', "not json"]):
			frappe.get_doc({"doctype": "Messaging Automation Run", "automation": name, "status": "Completed", "log": log,
				"sent_count": 1 - i}).insert(ignore_permissions=True, ignore_links=True)
		runs = api.get_runs(name)
		self.assertEqual(len(runs), 2)
		self.assertEqual(runs[0]["log"], [])  # newest = the corrupt one
		self.assertEqual(runs[1]["log"][0]["result"], "sent")
		self.assertEqual(len(api.get_runs(name, limit=1)), 1)

	def test_get_runs_negative_limit(self):
		name = api.save_automation(self._payload())["name"]
		runs = api.get_runs(name, limit=-1)
		self.assertIsInstance(runs, list)

	def test_paging_and_total_count(self):
		for i in range(3):
			api.save_automation(self._payload(title=f"ZZ page {i}"))
		first = api.get_automations(search="ZZ page", page_length=2)
		self.assertEqual((len(first["items"]), first["total_count"]), (2, 3))
		rest = api.get_automations(search="ZZ page", start=2, page_length=2)
		self.assertEqual(len(rest["items"]), 1)
		self.assertNotIn(rest["items"][0]["name"], [r["name"] for r in first["items"]])
		self.assertEqual(len(api.get_automations(search="ZZ page")["items"]), 3)  # page_length 0 = all
		self.assertNotIn("stats", first)

	def test_server_side_filters(self):
		name = api.save_automation(self._payload(title="ZZ filter"))["name"]
		self.assertIn(name, [r["name"] for r in api.get_automations(search="ZZ filter", trigger_type="Field Change", enabled="0", reference_doctype="ToDo")["items"]])
		self.assertEqual(api.get_automations(search="ZZ filter", enabled="1")["items"], [])
		self.assertEqual(api.get_automations(search="ZZ filter", trigger_type="Schedule")["items"], [])

	def test_get_stats_is_global(self):
		before = api.get_stats()
		api.save_automation(self._payload(title="ZZ stats"))
		after = api.get_stats()
		self.assertEqual(after["total"], before["total"] + 1)
		self.assertEqual(after["active"], before["active"])  # saved paused
		self.assertEqual(after["by_trigger"].get("Field Change", 0), before["by_trigger"].get("Field Change", 0) + 1)
		self.assertIn("ToDo", after["doctypes"])
		self.assertEqual(after["doctypes"], sorted(after["doctypes"]))
