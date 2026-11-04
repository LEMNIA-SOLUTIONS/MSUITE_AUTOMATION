"""Shim — remove next release. Field Change triggers replace this hook."""

from msuite_automation.msuite_automation.engine import triggers


def on_doc_status_change(doc, method=None):
	triggers.on_doc_event(doc, method or "on_update")
