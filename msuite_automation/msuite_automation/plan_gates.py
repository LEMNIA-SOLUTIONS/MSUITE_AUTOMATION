"""This app's plan limits. Core's plan_enforcer reads them through the msuite_plan_gates hook."""

from __future__ import annotations

import frappe
from msuite_automation.msuite_automation.constants import plan_features_for
from msuite_core.services.plans.plan_enforcer import Gate


def _skip_automation_active(doc) -> bool:
	"""Skip active gate if automation is disabled (doctype default is disabled=1) or if an existing automation was already active."""
	if getattr(doc, "disabled", 1):
		return True
	if not getattr(doc, "name", None) or (hasattr(doc, "is_new") and doc.is_new()):
		return False
	# If existing record was already active in DB, editing it doesn't consume an extra active slot
	try:
		if frappe.db.get_value("Messaging Automation", doc.name, "disabled") == 0:
			return True
	except Exception:
		pass
	return False


GATES = {
	# One doctype, three plan features — picked by automation type.
	# Also gated by AUTOMATION product limit on active automations.
	"Messaging Automation": Gate(
		feature="notifications",
		feature_for=plan_features_for,
		product="WA",
		label="Messaging Automation",
		on_methods=("before_insert", "before_save"),
		extra_gates=(
			Gate(
				feature="automation_active",
				product="AUTOMATION",
				label="Active Automations",
				count_doctype="Messaging Automation",
				period="total",
				filters={"disabled": 0},
				skip_if=_skip_automation_active,
				on_methods=("before_insert", "before_save"),
			),
		),
	),
	"Messaging Automation Run": Gate(
		feature="automation_runs_per_month",
		product="AUTOMATION",
		label="Automation Runs",
		count_doctype="Messaging Automation Run",
		period="month",
		skip_if=lambda doc: bool(getattr(doc, "name", None) and not doc.is_new()),
		on_methods=("before_insert",),
	),
}
