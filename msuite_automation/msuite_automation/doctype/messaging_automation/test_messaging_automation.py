import unittest

import frappe


class TestControllerValidation(unittest.TestCase):
	def tearDown(self):
		frappe.db.rollback()

	def _doc(self, **kw):
		base = {"doctype": "Messaging Automation", "title": "ZZ ctl", "disabled": 1,
			"triggers": [{"trigger_id": "t1", "trigger_type": "Field Change", "reference_doctype": "ToDo", "field": "status", "to_value": "Closed"}],
			"steps": [{"step_id": "s1", "step_type": "Send SMS", "sms_message": "hi"}],
			"edges": [{"source_id": "t1", "source_handle": "", "target_id": "s1"}]}
		base.update(kw)
		doc = frappe.get_doc(base)
		doc.flags.ignore_plan_gate = True  # plan limits are tested in test_plan_enforcer
		return doc

	def test_valid_graph_saves(self):
		self._doc().insert(ignore_permissions=True)

	def test_loop_rejected(self):
		d = self._doc(steps=[{"step_id": "a", "step_type": "Send SMS", "sms_message": "x"}, {"step_id": "b", "step_type": "Send SMS", "sms_message": "y"}],
			edges=[{"source_id": "t1", "target_id": "a"}, {"source_id": "a", "target_id": "b"}, {"source_id": "b", "target_id": "a"}])
		with self.assertRaisesRegex(frappe.ValidationError, "loop"):
			d.insert(ignore_permissions=True)

	def test_trigger_kind_fields_required(self):
		d = self._doc(triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo"}])
		with self.assertRaisesRegex(frappe.ValidationError, "Event is required"):
			d.insert(ignore_permissions=True)

	def test_field_checked_on_every_reaching_doctype(self):
		d = self._doc(
			triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"},
				{"trigger_id": "t2", "trigger_type": "Document Event", "reference_doctype": "Note", "doc_event": "After Save"}],
			steps=[{"step_id": "u", "step_type": "Update Field", "update_field": "description", "update_value": "x"}],
			edges=[{"source_id": "t1", "target_id": "u"}, {"source_id": "t2", "target_id": "u"}])
		with self.assertRaisesRegex(frappe.ValidationError, "description does not exist on Note"):
			d.insert(ignore_permissions=True)

	def test_filter_checked_on_its_trigger_doctype(self):
		d = self._doc(filters=[{"trigger_id": "t1", "field": "nope_field", "operator": "=", "value": "x"}])
		with self.assertRaisesRegex(frappe.ValidationError, "nope_field does not exist on ToDo"):
			d.insert(ignore_permissions=True)

	def test_update_field_rejects_protected_fields(self):
		d = self._doc(steps=[{"step_id": "s1", "step_type": "Update Field", "update_field": "docstatus", "update_value": "2"}])
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be updated"):
			d.insert(ignore_permissions=True)

	def test_non_dict_condition_rule_raises_validation_error(self):
		d = self._doc(steps=[{"step_id": "s1", "step_type": "If/Else", "condition_rules": '["x"]'}])
		with self.assertRaisesRegex(frappe.ValidationError, "condition rule must be a dict"):
			d.insert(ignore_permissions=True)

	def test_ids_generated(self):
		d = self._doc(steps=[{"step_type": "Send SMS", "sms_message": "hi"}], edges=[], notes=[{"text": "hello"}])
		d.insert(ignore_permissions=True)
		self.assertTrue(d.steps[0].step_id and d.notes[0].note_id)

	def test_activation_needs_mapping_per_reaching_doctype(self):
		from unittest.mock import patch

		d = self._doc(disabled=0,
			triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"},
				{"trigger_id": "t2", "trigger_type": "Document Event", "reference_doctype": "Note", "doc_event": "After Save"}],
			steps=[{"step_id": "w", "step_type": "Send WhatsApp", "template": "tpl_x"}],
			edges=[{"source_id": "t1", "target_id": "w"}, {"source_id": "t2", "target_id": "w"}],
			variable_mappings=[{"step_id": "w", "reference_doctype": "ToDo", "variable_key": "1", "source_field": "description"}])
		orig = frappe.db.get_value

		def fake_get_value(*args, **kwargs):
			if args and args[0] == "WhatsApp Templates":
				return frappe._dict(template="Hi {{1}}")
			return orig(*args, **kwargs)

		with patch("frappe.db.get_value", side_effect=fake_get_value):
			with self.assertRaisesRegex(frappe.ValidationError, r"map \{\{1\}\} for Note"):
				d.insert(ignore_permissions=True, ignore_links=True)

	def test_activation_variable_mapping_requires_source_field_or_static_fallback(self):
		from unittest.mock import patch

		orig = frappe.db.get_value

		def fake_get_value(*args, **kwargs):
			if args and args[0] == "WhatsApp Templates":
				return frappe._dict(template="Hi {{1}}")
			return orig(*args, **kwargs)

		# row with source_field='' and static_fallback='' -> activation issue
		d_empty = self._doc(
			disabled=0,
			triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
			steps=[{"step_id": "w", "step_type": "Send WhatsApp", "template": "tpl_x"}],
			edges=[{"source_id": "t1", "target_id": "w"}],
			variable_mappings=[{"step_id": "w", "reference_doctype": "ToDo", "variable_key": "1", "source_field": "", "static_fallback": ""}],
		)
		with patch("frappe.db.get_value", side_effect=fake_get_value):
			with self.assertRaisesRegex(frappe.ValidationError, r"map \{\{1\}\} for ToDo"):
				d_empty.insert(ignore_permissions=True, ignore_links=True)

		# row with static_fallback='Hi' -> no issue
		d_fallback = self._doc(
			disabled=0,
			triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
			steps=[{"step_id": "w", "step_type": "Send WhatsApp", "template": "tpl_x"}],
			edges=[{"source_id": "t1", "target_id": "w"}],
			variable_mappings=[{"step_id": "w", "reference_doctype": "ToDo", "variable_key": "1", "source_field": "", "static_fallback": "Hi"}],
		)
		with patch("frappe.db.get_value", side_effect=fake_get_value):
			d_fallback.insert(ignore_permissions=True, ignore_links=True)

	def test_filter_with_unknown_trigger_id_raises(self):
		d = self._doc(filters=[{"trigger_id": "unknown_trigger", "field": "status", "operator": "=", "value": "x"}])
		with self.assertRaisesRegex(frappe.ValidationError, "Filter references unknown trigger unknown_trigger"):
			d.insert(ignore_permissions=True)

	def test_variable_mapping_with_blank_doctype_checks_all_reaching_doctypes(self):
		d = self._doc(
			triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "ToDo", "doc_event": "After Save"}],
			steps=[{"step_id": "s1", "step_type": "Send WhatsApp", "template": "tpl_x"}],
			edges=[{"source_id": "t1", "target_id": "s1"}],
			variable_mappings=[{"step_id": "s1", "reference_doctype": "", "variable_key": "1", "source_field": "bogus_field"}],
		)
		with self.assertRaisesRegex(frappe.ValidationError, "Step s1 variable: field bogus_field does not exist on ToDo"):
			d.insert(ignore_permissions=True, ignore_links=True)

	def test_send_to_string_saves_without_crash(self):
		d = self._doc(steps=[{"step_id": "s1", "step_type": "Send SMS", "sms_message": "hi", "send_to": '"phone"'}])
		d.insert(ignore_permissions=True)

	def test_activation_refuses_number_from_other_account(self):
		from unittest.mock import patch

		d = self._doc(disabled=0, whatsapp_account="acc_x",
			steps=[{"step_id": "w", "step_type": "Send WhatsApp", "template": "tpl_x", "whatsapp_phone": "P9"}],
			edges=[{"source_id": "t1", "target_id": "w"}])
		orig = frappe.db.get_value

		def fake_get_value(*args, **kwargs):
			if args and args[0] == "WhatsApp Templates":
				return frappe._dict(template="Hi")
			return orig(*args, **kwargs)

		with patch("frappe.db.get_value", side_effect=fake_get_value):
			with self.assertRaisesRegex(frappe.ValidationError, "sending number P9 is not on account acc_x"):
				d.insert(ignore_permissions=True, ignore_links=True)

	def _wa_doc(self, **step):
		return self._doc(disabled=0,
			steps=[{"step_id": "w", "step_type": "Send WhatsApp", "template": "tpl_x", **step}],
			edges=[{"source_id": "t1", "target_id": "w"}])

	def _fake_template(self, **tpl):
		from unittest.mock import patch

		orig = frappe.db.get_value

		def fake_get_value(*args, **kwargs):
			if args and args[0] == "WhatsApp Templates":
				return frappe._dict(template="Hi", **tpl)
			return orig(*args, **kwargs)

		return patch("frappe.db.get_value", side_effect=fake_get_value)

	def test_activation_refuses_media_template_without_file(self):
		with self._fake_template(header_type="DOCUMENT", media_sample=None):
			with self.assertRaisesRegex(frappe.ValidationError, "template tpl_x needs a header document but has none"):
				self._wa_doc().insert(ignore_permissions=True, ignore_links=True)

	def test_activation_allows_media_template_with_file_or_own_header(self):
		with self._fake_template(header_type="DOCUMENT", media_sample="/files/a.pdf"):
			self._wa_doc().insert(ignore_permissions=True, ignore_links=True)
		frappe.db.rollback()
		with self._fake_template(header_type="DOCUMENT", media_sample=None):
			self._wa_doc(header_source="Document Print").insert(ignore_permissions=True, ignore_links=True)

	def test_send_to_field_validation(self):
		"""Validate send_to field requirements for WhatsApp/SMS/List (Phone) and Email."""
		# WhatsApp with Email field -> rejected
		d_wa_bad = self._doc(
			triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Sales Order", "doc_event": "After Save"}],
			steps=[{"step_id": "w1", "step_type": "Send WhatsApp", "template": "tpl_x", "send_to": '{"Sales Order": "contact_email"}'}],
			edges=[{"source_id": "t1", "target_id": "w1"}],
		)
		with self.assertRaisesRegex(frappe.ValidationError, "not a valid phone field"):
			d_wa_bad.insert(ignore_permissions=True, ignore_links=True)

		# WhatsApp with Phone field (contact_mobile) -> accepted
		d_wa_ok = self._doc(
			triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Sales Order", "doc_event": "After Save"}],
			steps=[{"step_id": "w1", "step_type": "Send WhatsApp", "template": "tpl_x", "send_to": '{"Sales Order": "contact_mobile"}'}],
			edges=[{"source_id": "t1", "target_id": "w1"}],
		)
		d_wa_ok.insert(ignore_permissions=True, ignore_links=True)
		frappe.db.rollback()

		# Delivery Note with sales_order.contact_mobile -> accepted
		d_dn_ok = self._doc(
			triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Delivery Note", "doc_event": "After Save"}],
			steps=[{"step_id": "w1", "step_type": "Send WhatsApp", "template": "tpl_x", "send_to": '{"Delivery Note": "sales_order.contact_mobile"}'}],
			edges=[{"source_id": "t1", "target_id": "w1"}],
		)
		d_dn_ok.insert(ignore_permissions=True, ignore_links=True)
		frappe.db.rollback()

		# Delivery Note with sales_order.contact_email for WhatsApp -> rejected
		d_dn_bad = self._doc(
			triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Delivery Note", "doc_event": "After Save"}],
			steps=[{"step_id": "w1", "step_type": "Send WhatsApp", "template": "tpl_x", "send_to": '{"Delivery Note": "sales_order.contact_email"}'}],
			edges=[{"source_id": "t1", "target_id": "w1"}],
		)
		with self.assertRaisesRegex(frappe.ValidationError, "not a valid phone field"):
			d_dn_bad.insert(ignore_permissions=True, ignore_links=True)

		# Email step with phone field -> rejected
		d_em_bad = self._doc(
			triggers=[{"trigger_id": "t1", "trigger_type": "Document Event", "reference_doctype": "Sales Order", "doc_event": "After Save"}],
			steps=[{"step_id": "e1", "step_type": "Send Email", "email_subject": "Sub", "send_to": '{"Sales Order": "contact_mobile"}'}],
			edges=[{"source_id": "t1", "target_id": "e1"}],
		)
		with self.assertRaisesRegex(frappe.ValidationError, "not a valid email field"):
			d_em_bad.insert(ignore_permissions=True, ignore_links=True)

	def test_update_value_required_field_validation(self):
		"""Update Field rejects blank value for required target field, allows for optional."""
		# Required field (description on ToDo) with blank value -> rejected
		d_req_blank = self._doc(
			steps=[{"step_id": "u1", "step_type": "Update Field", "update_field": "description", "update_value": ""}],
			edges=[{"source_id": "t1", "target_id": "u1"}],
		)
		with self.assertRaisesRegex(frappe.ValidationError, "cannot be blank"):
			d_req_blank.insert(ignore_permissions=True)

		# Required field with value -> accepted
		d_req_val = self._doc(
			steps=[{"step_id": "u1", "step_type": "Update Field", "update_field": "description", "update_value": "New Task"}],
			edges=[{"source_id": "t1", "target_id": "u1"}],
		)
		d_req_val.insert(ignore_permissions=True)
		frappe.db.rollback()

		# Optional field (status on ToDo) with blank value -> accepted
		d_opt_blank = self._doc(
			steps=[{"step_id": "u1", "step_type": "Update Field", "update_field": "status", "update_value": ""}],
			edges=[{"source_id": "t1", "target_id": "u1"}],
		)
		d_opt_blank.insert(ignore_permissions=True)
		frappe.db.rollback()

	def test_draft_with_unmapped_variables_is_blocked(self):
		"""AUT-101: unmapped WhatsApp variables block saving a draft; mapped (source or fallback) drafts save and activate."""
		from unittest.mock import patch

		orig = frappe.db.get_value

		def fake_get_value(*args, **kwargs):
			if args and args[0] == "WhatsApp Templates":
				return frappe._dict(template="Hello {{1}} and {{2}}")
			return orig(*args, **kwargs)

		def make(mappings):
			return self._doc(
				disabled=1,
				steps=[{"step_id": "w1", "step_type": "Send WhatsApp", "template": "tpl_x"}],
				edges=[{"source_id": "t1", "target_id": "w1"}],
				variable_mappings=mappings,
			)

		with patch("frappe.db.get_value", side_effect=fake_get_value):
			with self.assertRaisesRegex(frappe.ValidationError, r"map \{\{1\}\}"):
				make([]).insert(ignore_permissions=True, ignore_links=True)
			with self.assertRaisesRegex(frappe.ValidationError, r"map \{\{2\}\}"):
				make([{"step_id": "w1", "variable_key": "1", "source_field": "description"}]).insert(ignore_permissions=True, ignore_links=True)

			draft = make([
				{"step_id": "w1", "variable_key": "1", "source_field": "description"},
				{"step_id": "w1", "variable_key": "2", "static_fallback": "x"},
			])
			draft.insert(ignore_permissions=True, ignore_links=True)

			# Activation still runs its own gates (mapping no longer among them)
			draft.disabled = 0
			draft.variable_mappings = []
			with self.assertRaisesRegex(frappe.ValidationError, r"map \{\{1\}\}"):
				draft.save(ignore_permissions=True)

