"""bench --site <site> execute msuite_automation.msuite_automation.tests.verify_local_migration.run"""

import frappe


def run():
	ma = frappe.get_all("Messaging Automation", fields=["name", "trigger_type", "disabled"])
	no_type = [r.name for r in ma if not r.trigger_type]
	no_steps = [r.name for r in ma if not frappe.db.exists("Automation Step", {"parent": r.name})]
	orphan_steps = frappe.db.sql("""select s.parent, s.step_id from `tabAutomation Step` s
		left join `tabAutomation Step` p on p.parent = s.parent and p.step_id = s.parent_step_id
		where s.parent_step_id is not null and s.parent_step_id != '' and p.name is null""")
	print(f"automations: {len(ma)} | enabled: {sum(1 for r in ma if not r.disabled)}")
	print(f"by trigger: { {t: sum(1 for r in ma if r.trigger_type == t) for t in {r.trigger_type for r in ma}} }")
	print(f"missing trigger_type: {no_type}")
	print(f"without steps: {len(no_steps)} {no_steps[:20]}")
	print(f"orphan child steps: {orphan_steps}")
	print("review log:", frappe.get_all("Error Log", filters={"method": "Messaging Automation: needs review after migration"}, pluck="name"))
	assert not no_type and not orphan_steps, "migration incomplete"
