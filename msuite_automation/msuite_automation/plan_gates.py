"""This app's plan limits. Core's plan_enforcer reads them through the msuite_plan_gates hook."""

from __future__ import annotations

from msuite_automation.msuite_automation.constants import plan_features_for
from msuite_core.services.plans.plan_enforcer import Gate

GATES = {
	# One doctype, three plan features — picked by automation type.
	"Messaging Automation": Gate(
		feature="notifications",
		feature_for=plan_features_for,
		product="WA",
		label="Messaging Automation",
		on_methods=("before_insert", "before_save"),
	),
}
