"""Tests for WhatsApp template cleanup and unlinking across automations."""
import frappe
from frappe.tests import IntegrationTestCase

CLEANUP_TARGET_TEMPLATE = "tmpl-cleanup-target"
CLEANUP_UNAFFECTED_TEMPLATE = "tmpl-cleanup-unaffected"


class TestTemplateCleanup(IntegrationTestCase):
	"""Verify automations referencing a deleted WhatsApp template are disabled and unlinked."""

	def _create_automation(self, title: str, reference_doctype: str, template_name: str, step_id: str) -> str:
		"""Create a test Messaging Automation with a WhatsApp step referencing the given template.

		Args:
			title: Automation title.
			reference_doctype: Target DocType name.
			template_name: WhatsApp template name to attach to the step.
			step_id: Unique step identifier within the automation graph.

		Returns:
			str: The generated name of the created Messaging Automation document.

		Side Effects:
			Inserts a Messaging Automation and child Automation Step row into the database.
			Registers cleanup handlers to remove both after the test finishes.
		"""
		doc = frappe.get_doc({
			"doctype": "Messaging Automation",
			"title": title,
			"reference_doctype": reference_doctype,
			"disabled": 0,
			"steps": [
				{
					"step_id": step_id,
					"step_type": "Send WhatsApp",
					"template": template_name,
				}
			],
		})
		doc.flags.ignore_validate = True
		doc.flags.ignore_plan_gate = True
		doc.insert(ignore_permissions=True, ignore_links=True)

		self.addCleanup(frappe.db.delete, "Automation Step", {"parent": doc.name})
		self.addCleanup(frappe.db.delete, "Messaging Automation", {"name": doc.name})
		return doc.name

	def test_deleting_template_disables_every_automation_type(self):
		"""Confirm unlinking a template disables all referencing automations and unsets step template links.

		Args:
			None.

		Returns:
			None.

		Side Effects:
			Modifies database records via _cleanup_template_links and clears trigger cache.
		"""
		from msuite_whatsapp.whatsapp.doctype.whatsapp_templates.whatsapp_templates import _cleanup_template_links

		target_names = []
		for idx, ref in enumerate(("Sales Order", "CRM Lead")):
			name = self._create_automation(
				title=f"Tmpl Cleanup {ref}",
				reference_doctype=ref,
				template_name=CLEANUP_TARGET_TEMPLATE,
				step_id=f"step_target_{idx}",
			)
			target_names.append(name)

		unaffected_name = self._create_automation(
			title="Tmpl Cleanup Unaffected",
			reference_doctype="Sales Order",
			template_name=CLEANUP_UNAFFECTED_TEMPLATE,
			step_id="step_unaffected_0",
		)

		_cleanup_template_links(CLEANUP_TARGET_TEMPLATE)

		for name in target_names:
			disabled = frappe.db.get_value("Messaging Automation", name, "disabled")
			self.assertEqual(disabled, 1)

			step_templates = frappe.db.get_all(
				"Automation Step",
				filters={"parent": name, "parenttype": "Messaging Automation"},
				pluck="template",
			)
			self.assertEqual(step_templates, [None])

		# Unaffected automation should remain enabled and retain its template link
		self.assertEqual(frappe.db.get_value("Messaging Automation", unaffected_name, "disabled"), 0)
		unaffected_step_templates = frappe.db.get_all(
			"Automation Step",
			filters={"parent": unaffected_name, "parenttype": "Messaging Automation"},
			pluck="template",
		)
		self.assertEqual(unaffected_step_templates, [CLEANUP_UNAFFECTED_TEMPLATE])

	def test_cleanup_empty_or_none_template_is_noop(self):
		"""Ensure calling cleanup with an empty string or None does not error or alter records.

		Args:
			None.

		Returns:
			None.

		Side Effects:
			None.
		"""
		from msuite_whatsapp.whatsapp.doctype.whatsapp_templates.whatsapp_templates import _cleanup_template_links

		name = self._create_automation(
			title="Tmpl Cleanup Noop",
			reference_doctype="Sales Order",
			template_name=CLEANUP_TARGET_TEMPLATE,
			step_id="step_noop_0",
		)

		_cleanup_template_links("")
		_cleanup_template_links(None)

		self.assertEqual(frappe.db.get_value("Messaging Automation", name, "disabled"), 0)
		step_templates = frappe.db.get_all(
			"Automation Step",
			filters={"parent": name, "parenttype": "Messaging Automation"},
			pluck="template",
		)
		self.assertEqual(step_templates, [CLEANUP_TARGET_TEMPLATE])
