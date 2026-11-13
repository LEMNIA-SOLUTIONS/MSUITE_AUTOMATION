"""
Messaging Automation — one automation = trigger(s) + filters + a step tree.

Runtime lives in msuite_automation/engine/. The controller only validates and
keeps the trigger cache fresh.
"""

import json
import re

import frappe
from frappe import _
from frappe.model.document import Document

from msuite_automation.msuite_automation.constants import (
	ADD_TO_LIST,
	DATE_BASED,
	DOCUMENT_EVENT,
	EVERY_TIME,
	FIELD_CHANGE,
	IF_ELSE,
	OFFSET_DAYS_MAX,
	OFFSET_DAYS_MIN,
	SCHEDULE,
	SEND_EMAIL,
	SEND_SMS,
	SEND_WHATSAPP,
	SWITCH,
	UPDATE_FIELD,
)
from msuite_automation.msuite_automation.engine.graph import (
	Graph,
	_to_int,
	validate_activation,
	validate_graph,
	validate_warnings,
)
from msuite_automation.msuite_automation.validators.notification_validator import PROTECTED_PROPERTY_FIELDS

STANDARD_DOC_FIELDS = {"name", "owner", "modified", "creation", "docstatus"}
PROTECTED_UPDATE_FIELDS = PROTECTED_PROPERTY_FIELDS | STANDARD_DOC_FIELDS


def _json(raw, default):
	if not raw:
		return default
	try:
		return json.loads(raw) if isinstance(raw, str) else raw
	except ValueError:
		return default


class MessagingAutomation(Document):
	def validate(self):
		self._ensure_ids()
		self._validate_triggers()
		errors = validate_graph(self.triggers, self.steps, self.edges)
		if errors:
			frappe.throw("<br>".join(errors), title=_("Automation is not valid"))
		self._validate_fields_exist()
		# Unmapped template variables block every save (draft too), matching the builder's draft checklist.
		errors = self._mapping_issues()
		if errors:
			frappe.throw("<br>".join(errors), title=_("Automation is not valid"))
		if not self.disabled:
			errors = validate_activation(self.triggers, self.steps, self.edges) + self._number_issues() + self._header_issues() + self._schedule_issues()
			if errors:
				frappe.throw("<br>".join(errors), title=_("Automation can't be activated"))
		for warning in validate_warnings(self.triggers, self.steps, self.edges):
			frappe.msgprint(warning, alert=True, indicator="orange")

	def _ensure_ids(self):
		for row, key in [*((t, "trigger_id") for t in self.triggers), *((s, "step_id") for s in self.steps), *((n, "note_id") for n in self.notes)]:
			if not row.get(key):
				row.set(key, frappe.generate_hash(length=8))

	def _validate_triggers(self):
		for t in self.triggers:
			label = t.label or t.trigger_id
			kind = t.trigger_type
			if kind in (DOCUMENT_EVENT, FIELD_CHANGE, DATE_BASED) and not t.reference_doctype:
				frappe.throw(_("Trigger {0}: DocType is required").format(label))
			if kind == DOCUMENT_EVENT and not t.doc_event:
				frappe.throw(_("Trigger {0}: Event is required").format(label))
			if kind == FIELD_CHANGE:
				if not t.field:
					frappe.throw(_("Trigger {0}: field to watch is required").format(label))
				if t.from_value and t.to_value and t.from_value == t.to_value:
					frappe.throw(_("Trigger {0}: From and To cannot be the same").format(label))
			if kind == DATE_BASED:
				if not t.date_field:
					frappe.throw(_("Trigger {0}: Date field is required").format(label))
				offset = _to_int(t.offset_days)
				if offset is None or offset < OFFSET_DAYS_MIN or offset > OFFSET_DAYS_MAX:
					frappe.throw(_("Trigger {0}: offset days must be a whole number from {1} to {2}").format(label, OFFSET_DAYS_MIN, OFFSET_DAYS_MAX))
			if kind == SCHEDULE and not t.schedule_frequency:
				frappe.throw(_("Trigger {0}: Frequency is required").format(label))

	def _schedule_issues(self) -> list[str]:
		"""A Schedule trigger without a condition enqueues a run for EVERY
		document of its DocType on each tick (audit H7). Refuse the risky
		shapes before the automation can be activated."""
		issues = []
		for t in self.triggers:
			if t.trigger_type != SCHEDULE:
				continue
			label = t.label or t.trigger_id
			if t.schedule_frequency == "All":
				issues.append(_("Trigger {0}: frequency 'All' runs every few minutes — pick Hourly or slower").format(label))
			if t.condition:
				continue  # script mode decides its own recipients
			has_filter = any(f.trigger_id in ("", None, t.trigger_id) for f in (self.filters or []))
			if not has_filter:
				issues.append(_("Trigger {0}: add a filter or condition — otherwise every {1} record is messaged on each run").format(label, t.reference_doctype or _("matching")))
			if self.send_frequency == EVERY_TIME:
				issues.append(_("Trigger {0}: use 'Once per day' or 'Once per document' — 'Every time' re-sends to the same records on each run").format(label))
		return issues

	@staticmethod
	def _resolve_doctype_field(fieldname, doctype):
		"""Resolve fieldname and DocType, handling prefixes like sales_order. on Delivery Note.

		Args:
			fieldname: Name of the field, possibly prefixed.
			doctype: Name of the DocType.

		Returns:
			tuple[str, str]: Resolved (fieldname, doctype).

		Raises:
			None.

		Side Effects:
			None.
		"""
		if fieldname and doctype and fieldname.startswith("sales_order.") and doctype == "Delivery Note":
			return fieldname[len("sales_order."):], "Sales Order"
		return fieldname, doctype

	@classmethod
	def _check(cls, fieldname, doctype, what):
		"""Validate that a field exists on the target DocType.

		Args:
			fieldname: Name of the field to check.
			doctype: Target DocType name.
			what: Human-readable context descriptor for error messages.

		Raises:
			frappe.ValidationError: If the field does not exist.

		Side Effects:
			None.
		"""
		if not fieldname or not doctype:
			return
		fieldname, doctype = cls._resolve_doctype_field(fieldname, doctype)
		if fieldname not in STANDARD_DOC_FIELDS and not frappe.get_meta(doctype).has_field(fieldname):
			frappe.throw(_("{0}: field {1} does not exist on {2}").format(what, fieldname, doctype))

	def _validate_fields_exist(self):
		check = self._check
		trigger_by_id = {t.trigger_id: t for t in self.triggers if t.trigger_id}
		for t in self.triggers:
			check(t.field, t.reference_doctype, _("Trigger {0}").format(t.label or t.trigger_id))
			check(t.date_field, t.reference_doctype, _("Trigger {0}").format(t.label or t.trigger_id))
		for f in self.filters:
			if f.trigger_id:
				t = trigger_by_id.get(f.trigger_id)
				if not t:
					frappe.throw(_("Filter references unknown trigger {0}").format(f.trigger_id))
				check(f.field, t.reference_doctype, _("Trigger {0} filter").format(t.label or t.trigger_id))
			else:
				for t in self.triggers:
					check(f.field, t.reference_doctype, _("Trigger {0} filter").format(t.label or t.trigger_id))
		for r in self.recipients:
			check(r.phone_field, r.reference_doctype, _("Default recipient"))
			check(r.email_field, r.reference_doctype, _("Default recipient"))
		graph = Graph(self.triggers, self.steps, self.edges)
		for st in self.steps:
			what = _("Step {0}").format(st.step_id)
			if st.step_type == UPDATE_FIELD:
				if st.update_field in PROTECTED_UPDATE_FIELDS:
					frappe.throw(_("Step {0}: field {1} cannot be updated").format(st.step_id, st.update_field))
				for dt in graph.reaching_doctypes(st.step_id):
					fld_name, target_dt = self._resolve_doctype_field(st.update_field, dt)
					if fld_name and fld_name not in STANDARD_DOC_FIELDS and frappe.get_meta(target_dt).has_field(fld_name):
						df = frappe.get_meta(target_dt).get_field(fld_name)
						if df and df.reqd:
							val = st.update_value
							if val is None or str(val).strip() == "":
								frappe.throw(_("Step {0}: {1} is required and cannot be blank").format(st.step_id, df.label or fld_name))
			rules = _json(st.condition_rules, []) if st.step_type == IF_ELSE else []
			for rule in rules if isinstance(rules, list) else []:
				if not isinstance(rule, dict):
					frappe.throw(_("Step {0}: condition rule must be a dict").format(st.step_id))
			send_to = _json(st.send_to, {})
			send_to = send_to if isinstance(send_to, dict) else {}
			for dt in graph.reaching_doctypes(st.step_id):
				check(st.update_field, dt, what)
				check(st.switch_field if st.step_type == SWITCH else None, dt, what)
				for rule in rules if isinstance(rules, list) else []:
					check(rule.get("field"), dt, what)
				send_to_field = send_to.get(dt)
				check(send_to_field, dt, what)
				if send_to_field:
					fld_name, target_dt = self._resolve_doctype_field(send_to_field, dt)
					if fld_name and fld_name not in STANDARD_DOC_FIELDS and frappe.get_meta(target_dt).has_field(fld_name):
						df = frappe.get_meta(target_dt).get_field(fld_name)
						opts = (df.options or "").strip().lower()
						if st.step_type in (SEND_WHATSAPP, SEND_SMS, ADD_TO_LIST):
							is_phone = df.fieldtype == "Phone" or (df.fieldtype in ("Data", "Small Text") and opts == "phone")
							if not is_phone:
								frappe.throw(_("Step {0}: {1} on {2} is not a valid phone field").format(st.step_id, send_to_field, dt))
						elif st.step_type == SEND_EMAIL:
							is_email = df.fieldtype == "Email" or (df.fieldtype in ("Data", "Small Text") and opts == "email")
							if not is_email:
								frappe.throw(_("Step {0}: {1} on {2} is not a valid email field").format(st.step_id, send_to_field, dt))
		for m in self.variable_mappings:
			what = _("Step {0} variable").format(m.step_id)
			if m.reference_doctype:
				check(m.source_field, m.reference_doctype, what)
			else:
				for dt in graph.reaching_doctypes(m.step_id):
					check(m.source_field, dt, what)

	def _mapping_issues(self) -> list[str]:
		graph = Graph(self.triggers, self.steps, self.edges)
		issues = []
		for st in self.steps:
			if st.step_type != SEND_WHATSAPP or not st.template or st.delivery_mode == "Bulk campaign":
				continue
			tpl = frappe.db.get_value("WhatsApp Templates", st.template, ["template"], as_dict=True) or {}
			keys = list(dict.fromkeys(re.findall(r"\{\{(\w+)\}\}", tpl.get("template") or "")))
			for dt in sorted(graph.reaching_doctypes(st.step_id)):
				have = {
					m.variable_key
					for m in self.variable_mappings
					if m.step_id == st.step_id
					and m.reference_doctype in ("", None, dt)
					and (m.source_field or m.static_fallback)
				}
				issues += [_("WhatsApp step {0}: map {{{{{1}}}}} for {2}").format(st.step_id, k, dt) for k in keys if k not in have]
		return issues

	def _number_issues(self) -> list[str]:
		issues = []
		for st in self.steps:
			if st.step_type != SEND_WHATSAPP or not st.whatsapp_phone or st.delivery_mode == "Bulk campaign":
				continue
			acc = st.whatsapp_account or self.whatsapp_account
			if not acc or not frappe.db.exists("WhatsApp Phone Number", {"parenttype": "WhatsApp Account", "parent": acc, "phone_number_id": st.whatsapp_phone}):
				issues.append(_("WhatsApp step {0}: sending number {1} is not on account {2}").format(st.step_id, st.whatsapp_phone, acc or _("(none chosen)")))
		return issues

	def _header_issues(self) -> list[str]:
		issues = []
		for st in self.steps:
			if st.step_type != SEND_WHATSAPP or not st.template or (st.header_source or "None") != "None":
				continue
			tpl = frappe.db.get_value("WhatsApp Templates", st.template, ["header_type", "media_sample"], as_dict=True) or {}
			if tpl.get("header_type") in ("DOCUMENT", "IMAGE") and not tpl.get("media_sample"):
				issues.append(_("WhatsApp step {0}: template {1} needs a header document but has none on file — re-sync it under WhatsApp → Templates").format(st.step_id, st.template))
		return issues

	def _clear(self):
		from msuite_automation.msuite_automation.engine.triggers import clear_cache

		clear_cache()

	def on_update(self):
		self._clear()

	def on_trash(self):
		self._clear()

	def after_rename(self, old_name, new_name, merge=False):
		self._clear()
