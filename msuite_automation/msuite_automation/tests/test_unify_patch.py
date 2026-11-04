import unittest
from unittest.mock import patch

import frappe

from msuite_core.patches import merge_into_messaging_automation as merge
from msuite_core.patches import rename_variable_mapping_source_field as rename_patch
from msuite_core.patches import unify_messaging_automation as unify


class TestUnifyPatch(unittest.TestCase):
	def setUp(self):
		self._cleanup()
		self._patch_commit = patch.object(frappe.db, "commit")
		self._patch_commit.start()
		self._orig_legacy_rows = unify._legacy_rows
		self._patch_rows = patch.object(unify, "_legacy_rows", side_effect=self._only_zz_rows)
		self._patch_rows.start()

	def tearDown(self):
		self._patch_rows.stop()
		self._patch_commit.stop()
		self._cleanup()

	def _only_zz_rows(self):
		return [r for r in self._orig_legacy_rows() if str(r.get("name", "")).startswith("ZZ-LEGACY-")]

	def _cleanup(self):
		frappe.db.rollback()
		for name in ("ZZ-LEGACY-1", "ZZ-LEGACY-2", "ZZ-LEGACY-3", "ZZ-LEGACY-4", "ZZ-LEGACY-FAIL"):
			frappe.db.delete("Messaging Automation", {"name": name})
			frappe.db.delete("Automation Step", {"parent": name})
			frappe.db.delete("Automation Trigger", {"parent": name})
			frappe.db.delete("Automation Filter", {"parent": name})
			frappe.db.delete("Automation Variable Mapping", {"parent": name})

	def _legacy(self, name, **cols):
		# Insert straight into the table: legacy columns are not in meta any more.
		cols = {"name": name, "title": name, "disabled": 0, **cols}
		merge._insert(cols)
		return name

	def test_merge_insert_keeps_legacy_columns(self):
		self._legacy("ZZ-LEGACY-1", notification_type="Lead Status Event", reference_doctype="CRM Lead", to_status="Won", template="x")
		row = frappe.db.sql("select notification_type, to_status from `tabMessaging Automation` where name='ZZ-LEGACY-1'", as_dict=True)[0]
		self.assertEqual((row.notification_type, row.to_status), ("Lead Status Event", "Won"))

	def test_apply_converts_and_is_idempotent(self):
		tpl = frappe.db.get_value("WhatsApp Templates", {}, "name")
		if not tpl:
			self.skipTest("needs one WhatsApp Templates row")
		self._legacy("ZZ-LEGACY-2", notification_type="Lead Status Event", reference_doctype="CRM Lead", to_status="Won", template=tpl)
		with patch.object(unify, "_clear_cache"):
			unify.execute()
			unify.execute()
		doc = frappe.get_doc("Messaging Automation", "ZZ-LEGACY-2")
		self.assertEqual(doc.trigger_type, "Field Change")
		self.assertEqual([t.to_value for t in doc.triggers], ["Won"])
		self.assertEqual([s.step_type for s in doc.steps], ["Send WhatsApp"])
		self.assertEqual(doc.disabled, 0)

	def test_problem_rows_are_disabled(self):
		self._legacy("ZZ-LEGACY-3", notification_type="DocType Event", reference_doctype="ToDo", doctype_event="After Save")
		with patch.object(unify, "_clear_cache"):
			unify.execute()
		self.assertEqual(frappe.db.get_value("Messaging Automation", "ZZ-LEGACY-3", "disabled"), 1)

	def test_preview_writes_nothing(self):
		self._legacy("ZZ-LEGACY-4", notification_type="DocType Event", reference_doctype="ToDo", doctype_event="After Save", send_sms=1, sms_message="x")
		lines = unify.preview()
		self.assertTrue(any("ZZ-LEGACY-4" in l for l in lines))
		self.assertFalse(frappe.db.count("Automation Step", {"parent": "ZZ-LEGACY-4"}))

	def test_failed_apply_leaves_disabled_untouched_and_rolls_back_savepoint(self):
		self._legacy("ZZ-LEGACY-FAIL", notification_type="Lead Status Event", reference_doctype="CRM Lead",
			to_status="Won", template="tpl", disabled=0)
		with patch.object(unify, "_clear_cache"), \
			patch.object(unify, "apply", side_effect=Exception("DB constraint error")), \
			patch.object(frappe, "log_error") as mock_log:
			unify.execute()
		self.assertEqual(frappe.db.get_value("Messaging Automation", "ZZ-LEGACY-FAIL", "disabled"), 0)
		self.assertEqual(frappe.db.count("Automation Step", {"parent": "ZZ-LEGACY-FAIL"}), 0)
		mock_log.assert_called_once()
		log_msg = mock_log.call_args[1].get("message", "")
		self.assertIn("NOT converted", log_msg)
		self.assertIn("bench --site <site> execute msuite_core.patches.unify_messaging_automation.execute", log_msg)

	@patch.object(frappe.db, "table_exists", return_value=True)
	@patch.object(frappe.db, "get_table_columns", return_value=["lead_field", "source_field", "name"])
	@patch.object(frappe.db, "sql")
	@patch.object(frappe.db, "sql_ddl")
	def test_rename_case_b_both_exist(self, mock_ddl, mock_sql, mock_cols, mock_exists):
		rename_patch.execute()
		mock_sql.assert_called_with("update `tabAutomation Variable Mapping` set source_field = lead_field where ifnull(source_field, '') = ''")
		mock_ddl.assert_called_with("alter table `tabAutomation Variable Mapping` drop column `lead_field`")

	@patch.object(frappe.db, "table_exists", return_value=True)
	@patch.object(frappe.db, "get_table_columns", return_value=["lead_field", "name"])
	@patch.object(frappe.db, "sql_ddl")
	def test_rename_case_a_lead_field_only(self, mock_ddl, mock_cols, mock_exists):
		rename_patch.execute()
		mock_ddl.assert_called_with("alter table `tabAutomation Variable Mapping` change `lead_field` `source_field` varchar(140)")

	@patch.object(frappe.db, "table_exists", return_value=True)
	@patch.object(frappe.db, "get_table_columns", return_value=["source_field", "name"])
	@patch.object(frappe.db, "sql_ddl")
	def test_rename_case_c_lead_field_missing(self, mock_ddl, mock_cols, mock_exists):
		rename_patch.execute()
		mock_ddl.assert_not_called()
