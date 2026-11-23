# Copyright (c) 2026, MSuite and contributors
# For license information, please see license.txt

"""Patch: Repoint Automation Step links to Broadcast and Recipient List.

Repoints legacy Bulk WhatsApp Message links in Automation Step.target_bulk_message
to unified Broadcast records, and WhatsApp Recipient List links in
Automation Step.recipient_list to unified Recipient List records.
"""

from __future__ import annotations

import frappe
from msuite_campaigns.broadcast.legacy import resolve


def execute(dry_run: bool = True) -> dict:
	"""Repoint Automation Step legacy links to Broadcast and Recipient List.

	Args:
		dry_run: When True (default), does not write changes to the database.
			When False, commits the repointed values.

	Returns:
		dict: Summary with keys:
			{
				"checked": int,
				"repointed": int,
				"unresolved": list[str],
				"dry_run": bool,
			}
	"""
	summary: dict = {
		"checked": 0,
		"repointed": 0,
		"unresolved": [],
		"dry_run": dry_run,
	}

	steps = frappe.db.get_all(
		"Automation Step",
		fields=["name", "target_bulk_message", "recipient_list"],
	)

	for step in steps:
		updates: dict[str, str] = {}

		# 1. target_bulk_message -> Broadcast
		target_val = step.get("target_bulk_message")
		if target_val:
			summary["checked"] += 1
			new_bc = (
				resolve("Bulk WhatsApp Message", target_val)
				or resolve("Bulk Email Message", target_val)
				or resolve("Bulk SMS Message", target_val)
			)
			if new_bc:
				if new_bc != target_val:
					updates["target_bulk_message"] = new_bc
					summary["repointed"] += 1
			else:
				if not frappe.db.exists("Broadcast", target_val):
					summary["unresolved"].append(target_val)

		# 2. recipient_list -> Recipient List
		list_val = step.get("recipient_list")
		if list_val:
			summary["checked"] += 1
			new_rl = (
				resolve("WhatsApp Recipient List", list_val)
				or resolve("Email Recipient List", list_val)
				or resolve("Audience", list_val)
			)
			if new_rl:
				if new_rl != list_val:
					updates["recipient_list"] = new_rl
					summary["repointed"] += 1
			else:
				if not frappe.db.exists("Recipient List", list_val):
					summary["unresolved"].append(list_val)

		if updates and not dry_run:
			frappe.db.set_value(
				"Automation Step",
				step.name,
				updates,
				update_modified=False,
			)

	if not dry_run:
		frappe.db.commit()

	return summary
