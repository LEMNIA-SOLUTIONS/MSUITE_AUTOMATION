"""Compatibility shims for background jobs queued before the unified engine
deployed. Remove next release (spec §4.6)."""

import frappe

from msuite_automation.msuite_automation.engine import executor, triggers


def dispatch_order_automation_workflow(workflow_name, node_id=None, doctype=None, docname=None, new_val=None, discriminator=None):
	executor.run_automation(workflow_name, node_id, doctype, docname, value=new_val or "", discriminator=discriminator or "")


def schedule_doc_event_notification(notification_name, doc, event=None):
	executor.enqueue_run(name=notification_name, trigger_id=None, doctype=doc.doctype, docname=doc.name,
		doc_dict=doc.as_dict() if event in ("on_trash", "after_delete") else None)


def run_server_script_for_doc_event(doc, event):
	triggers.on_doc_event(doc, event)


def on_order_status_changed(**kwargs):
	triggers.on_order_status_changed(**kwargs)
