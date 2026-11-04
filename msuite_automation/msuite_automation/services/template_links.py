import frappe


def unlink_whatsapp_template(template_name: str) -> None:
	"""Disable automations that used a WhatsApp template that is being removed.

	Registered in hooks.py as `whatsapp_template_removed_listeners`.
	"""
	if frappe.db.has_column("Messaging Automation", "template"):
		frappe.db.sql(
			"UPDATE `tabMessaging Automation` SET `disabled` = 1, `template` = NULL WHERE `template` = %s",
			template_name,
		)
	if frappe.db.has_table("Automation Step") and frappe.db.has_column("Automation Step", "template"):
		parents = frappe.db.sql_list(
			"SELECT DISTINCT parent FROM `tabAutomation Step` WHERE template = %s AND parenttype = 'Messaging Automation'",
			template_name,
		)
		if parents:
			frappe.db.sql(
				"UPDATE `tabMessaging Automation` SET `disabled` = 1 WHERE name IN %s",
				(tuple(parents),),
			)
			frappe.db.sql(
				"UPDATE `tabAutomation Step` SET template = NULL WHERE template = %s AND parenttype = 'Messaging Automation'",
				template_name,
			)
	from msuite_automation.msuite_automation.engine.triggers import clear_cache

	clear_cache()
