"""Tests for MSuite Automation plan gates (automation_runs_per_month, automation_active)."""

from __future__ import annotations

from unittest.mock import MagicMock, patch
import frappe
from frappe.tests import IntegrationTestCase

from msuite_core.services.plans.plan_enforcer import (
	enforce_gate,
	get_gates,
)


class TestAutomationPlanGates(IntegrationTestCase):
	"""Test suite for AUTOMATION product gates."""

	def setUp(self):
		super().setUp()
		self.orig_in_test = getattr(frappe.flags, "in_test", False)
		if hasattr(frappe.local, "_plan_gate_warnings"):
			delattr(frappe.local, "_plan_gate_warnings")

	def tearDown(self):
		frappe.flags.in_test = self.orig_in_test
		super().tearDown()

	def test_automation_runs_per_month_gate_registered(self):
		"""Messaging Automation Run must be registered in get_gates()."""
		gates = get_gates()
		self.assertIn("Messaging Automation Run", gates)
		gate = gates["Messaging Automation Run"]
		self.assertEqual(gate.feature, "automation_runs_per_month")
		self.assertEqual(gate.product, "AUTOMATION")
		self.assertEqual(gate.period, "month")
		self.assertEqual(gate.count_doctype, "Messaging Automation Run")

	def test_automation_runs_per_month_at_cap_blocks_insert(self):
		"""When monthly automation runs usage is at cap, run insert is blocked."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "AUTOMATION-TRIAL",
			"products": ["AUTOMATION"],
			"features": {"automation_runs_per_month": {"enabled": True, "limit": 100}},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			with patch("msuite_core.services.plans.plan_enforcer.get_current_usage", return_value=100):
				doc = MagicMock()
				doc.doctype = "Messaging Automation Run"
				doc.name = "RUN-001"
				doc.flags.ignore_plan_gate = False
				doc.is_new.return_value = True

				with self.assertRaises(frappe.ValidationError) as ctx:
					enforce_gate(doc, "before_insert")
				self.assertIn("Automation Runs limit reached (100/100 this month)", str(ctx.exception))

	def test_automation_runs_per_month_under_cap_allows_insert(self):
		"""When monthly automation runs usage is under cap, run insert succeeds."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "AUTOMATION-TRIAL",
			"products": ["AUTOMATION"],
			"features": {"automation_runs_per_month": {"enabled": True, "limit": 100}},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			with patch("msuite_core.services.plans.plan_enforcer.get_current_usage", return_value=50):
				doc = MagicMock()
				doc.doctype = "Messaging Automation Run"
				doc.name = "RUN-001"
				doc.flags.ignore_plan_gate = False
				doc.is_new.return_value = True

				enforce_gate(doc, "before_insert")

	def test_automation_active_gate_registered(self):
		"""Messaging Automation gates include automation_active check."""
		gates = get_gates()
		self.assertIn("Messaging Automation", gates)
		gate = gates["Messaging Automation"]
		all_features = [gate.feature] + [eg.feature for eg in getattr(gate, "extra_gates", ())]
		self.assertIn("automation_active", all_features)

	def test_automation_active_at_cap_blocks_activating(self):
		"""When active automations are at cap, saving a new active automation is blocked."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "AUTOMATION-TRIAL",
			"products": ["AUTOMATION", "WA"],
			"features": {
				"notifications": {"enabled": True, "limit": None},
				"automation_active": {"enabled": True, "limit": 1},
			},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			with patch("msuite_core.services.plans.plan_enforcer.get_current_usage", return_value=1):
				doc = MagicMock()
				doc.doctype = "Messaging Automation"
				doc.name = "MA-NEW"
				doc.disabled = 0
				doc.flags.ignore_plan_gate = False
				doc.is_new.return_value = True

				with self.assertRaises(frappe.ValidationError) as ctx:
					enforce_gate(doc, "before_insert")
				self.assertIn("Active Automations limit reached (1/1 total)", str(ctx.exception))

	def test_automation_active_allows_disabled_automation(self):
		"""Saving a disabled automation (disabled=1) when active is at cap is allowed."""
		frappe.flags.in_test = False
		plan_payload = {
			"plan_name": "AUTOMATION-TRIAL",
			"products": ["AUTOMATION", "WA"],
			"features": {
				"notifications": {"enabled": True, "limit": None},
				"automation_active": {"enabled": True, "limit": 1},
			},
		}
		with patch("msuite_core.services.plans.plan_enforcer.get_plan_data", return_value=plan_payload):
			with patch("msuite_core.services.plans.plan_enforcer.get_current_usage", return_value=1):
				doc = MagicMock()
				doc.doctype = "Messaging Automation"
				doc.name = "MA-DISABLED"
				doc.disabled = 1
				doc.flags.ignore_plan_gate = False
				doc.is_new.return_value = True

				# Must not raise
				enforce_gate(doc, "before_insert")
