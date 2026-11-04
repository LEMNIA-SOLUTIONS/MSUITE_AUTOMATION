import frappe
from frappe.tests import IntegrationTestCase


class TestTemplateCleanup(IntegrationTestCase):
	def test_deleting_template_disables_every_automation_type(self):
		from msuite_whatsapp.whatsapp.doctype.whatsapp_templates.whatsapp_templates import _cleanup_template_links

		names = []
		for ntype, ref in (("Order Workflow Event", "Sales Order"), ("Lead Status Event", "CRM Lead")):
			doc = frappe.new_doc("Messaging Automation")
			doc.update({"title": f"Tmpl Cleanup {ntype}", "notification_type": ntype, "reference_doctype": ref,
				"template": "tmpl-cleanup-x", "disabled": 0})
			doc.flags.ignore_plan_gate = True
			doc.db_insert()
			frappe.db.sql("UPDATE `tabMessaging Automation` SET template = 'tmpl-cleanup-x' WHERE name = %s", doc.name)
			names.append(doc.name)
			self.addCleanup(frappe.db.delete, "Messaging Automation", doc.name)

		_cleanup_template_links("tmpl-cleanup-x")

		for n in names:
			self.assertEqual(frappe.db.get_value("Messaging Automation", n, ["disabled", "template"]), (1, None))
