app_name = "msuite_automation"
app_title = "MSuite Automation"
app_publisher = "MSuite"
app_description = "Messaging Automation: visual flows, triggers, the run engine, send steps and the event outbox."
app_email = "dev@msuite.com"
app_license = "mit"

required_apps = ["frappe", "msuite_core", "msuite_campaigns", "msuite_inbox"]

before_install = "msuite_automation.install.before_install"

PE = "msuite_core.services.plans.plan_enforcer"

# This app's plan limits, read by msuite_core.services.plans.plan_enforcer.
msuite_plan_gates = ["msuite_automation.msuite_automation.plan_gates.GATES"]

# The Inbox tells this app when an Instagram consent reply changes.
instagram_consent_changed_listeners = ["msuite_automation.msuite_automation.engine.consent_waits.wake_waits"]

doc_events = {
	# Messaging Automation: DocType Event rules on ANY doctype. The handler's
	# first step is a cached-map lookup, so doctypes without rules cost one
	# request-local cache read. See msuite_automation/engine/triggers.py.
	"*": {
		ev: "msuite_automation.msuite_automation.engine.triggers.on_doc_event"
		for ev in (
			"before_insert", "before_validate", "validate", "after_insert", "on_update",
			"before_submit", "on_submit", "before_cancel", "on_cancel", "on_trash", "after_delete",
			"before_update_after_submit", "on_update_after_submit",
		)
	},
	"Messaging Automation": {"before_insert": f"{PE}.enforce_gate", "before_save": f"{PE}.enforce_gate"},
}

# ── Order Notifications: subscribe to the order app's order_status_changed event ──
# The order app never imports this app (see Global Constraints in
# docs/superpowers/plans/2026-09-15-order-notifications-engine.md) — this is
# the decoupled subscription side, same dotted-path-list shape as
# campaign_event_listeners.
order_status_changed_listeners = [
	"msuite_automation.msuite_automation.engine.triggers.on_order_status_changed",
]

default_log_clearing_doctypes = {"Messaging Automation Run": 90}

scheduler_events = {
	"cron": {
		"*/5 * * * *": [
			"msuite_automation.msuite_automation.engine.executor.redeliver_outbox",
		],
		"* * * * *": [
			"msuite_automation.msuite_automation.engine.executor.resume_waiting_runs",
		],
		"*/10 * * * *": [
			"msuite_automation.msuite_automation.engine.executor.sweep_stuck_runs",
			"msuite_automation.msuite_automation.engine.executor.poll_event_waits",
		],
	},
	# Messaging Automation — Schedule triggers by frequency, the daily
	# Date Based sweep, and the per-minute Wait-step resume.
	**{
		key: [f"msuite_automation.msuite_automation.engine.triggers.schedule_{key}"]
		for key in ("all", "weekly", "monthly", "yearly", "hourly_long", "daily_long", "weekly_long", "monthly_long")
	},
	"hourly": [
		"msuite_automation.msuite_automation.engine.triggers.schedule_hourly",
	],
	"daily": [
		"msuite_automation.msuite_automation.engine.triggers.schedule_daily",
		"msuite_automation.msuite_automation.engine.triggers.run_date_based",
	],
}

whatsapp_template_removed_listeners = [
	"msuite_automation.msuite_automation.services.template_links.unlink_whatsapp_template",
]
