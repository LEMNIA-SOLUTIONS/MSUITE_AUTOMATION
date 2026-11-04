"""Regression tests for hooks.py doc_events wiring — cheap insurance against
a future edit silently clobbering an existing dict entry instead of merging
into it (this bit us once: see docs/superpowers/plans/2026-09-15-order-notifications-engine.md)."""

import frappe
from frappe.tests import IntegrationTestCase

ENTRY_POINT = "msuite_automation.msuite_automation.engine.triggers.on_doc_event"
OLD_ENTRY_POINTS = (
    "msuite_automation.msuite_automation.services.notification_service.run_server_script_for_doc_event",
    "msuite_whatsapp.utils.run_server_script_for_doc_event",
)


class TestOrderNotificationHooksWiring(IntegrationTestCase):
    def test_wildcard_doc_events_wired(self):
        events = frappe.get_hooks("doc_events", app_name="msuite_automation").get("*", {})
        self.assertIn(ENTRY_POINT, _as_list(events.get("after_insert")))

    def test_sales_order_after_insert_no_longer_listed(self):
        events = frappe.get_hooks("doc_events", app_name="msuite_commerce").get("Sales Order", {})
        self.assertNotIn(ENTRY_POINT, _as_list(events.get("after_insert")))
        for p in OLD_ENTRY_POINTS:
            self.assertNotIn(p, _as_list(events.get("after_insert")))

    def test_sales_order_on_cancel_still_present(self):
        """Guards against changes clobbering pre-existing Meta Commerce on_cancel hook."""
        events = frappe.get_hooks("doc_events", app_name="msuite_commerce").get("Sales Order", {})
        self.assertIn(
            "msuite_commerce.meta_ecommerce.services.order_cancellation.on_sales_order_cancel",
            _as_list(events.get("on_cancel")),
        )

    def test_delivery_note_after_insert_no_longer_listed(self):
        events = frappe.get_hooks("doc_events", app_name="msuite_commerce").get("Delivery Note", {})
        self.assertNotIn(ENTRY_POINT, _as_list(events.get("after_insert")))
        for p in OLD_ENTRY_POINTS:
            self.assertNotIn(p, _as_list(events.get("after_insert")))

    def test_delivery_note_on_submit_still_present(self):
        events = frappe.get_hooks("doc_events", app_name="msuite_commerce").get("Delivery Note", {})
        self.assertIn(
            "msuite_commerce.meta_ecommerce.services.order_fulfillment.on_delivery_note_submit",
            _as_list(events.get("on_submit")),
        )

    def test_messaging_automation_plan_gate_wired(self):
        events = frappe.get_hooks("doc_events", app_name="msuite_automation").get("Messaging Automation", {})
        gate = "msuite_core.services.plans.plan_enforcer.enforce_gate"
        self.assertIn(gate, _as_list(events.get("before_insert")))
        self.assertIn(gate, _as_list(events.get("before_save")))


def _as_list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]
