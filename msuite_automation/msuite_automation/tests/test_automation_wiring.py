"""Hook wiring for the unified automation engine."""

import importlib
import unittest

from msuite_automation import hooks

TR = "msuite_automation.msuite_automation.engine.triggers"
EX = "msuite_automation.msuite_automation.engine.executor"
EVENTS = ("before_insert", "before_validate", "validate", "after_insert", "on_update", "before_submit", "on_submit",
	"before_cancel", "on_cancel", "on_trash", "after_delete", "before_update_after_submit", "on_update_after_submit")


CRON_EVERY_FIVE_MINUTES = "*/5 * * * *"
CRON_EVERY_MINUTE = "* * * * *"


def _as_list(v):
	"""Return value as list if not already a list."""
	return v if isinstance(v, list) else [v]


def _all_handlers():
	"""Yield (doctype, event, handler) triples from doc_events hooks."""
	for dt, events in hooks.doc_events.items():
		for ev, h in events.items():
			for x in _as_list(h):
				yield dt, ev, x


class TestWiring(unittest.TestCase):
	def test_star_events(self):
		"""Verify standard document lifecycle events are wired to on_doc_event."""
		for ev in EVENTS:
			self.assertIn(f"{TR}.on_doc_event", _as_list(hooks.doc_events["*"][ev]))

	def test_no_legacy_handlers(self):
		"""Verify superseded legacy automation handlers are not present in hooks."""
		legacy = ("lead_status_automation.on_doc_status_change", "handle_doc_event_for_order_automation", "run_server_script_for_doc_event")
		self.assertFalse([h for _, _, h in _all_handlers() if h.endswith(legacy)])

	def test_schedulers(self):
		"""Verify scheduler events for triggers, runner, and outbox redelivery are configured."""
		ev = hooks.scheduler_events
		self.assertIn(f"{TR}.schedule_hourly", ev["hourly"])
		self.assertIn(f"{TR}.run_date_based", ev["daily"])
		self.assertIn(f"{EX}.resume_waiting_runs", ev["cron"][CRON_EVERY_MINUTE])
		self.assertIn(f"{EX}.redeliver_outbox", ev["cron"][CRON_EVERY_FIVE_MINUTES])
		self.assertEqual(hooks.order_status_changed_listeners, [f"{TR}.on_order_status_changed"])

	def test_every_hook_path_resolves(self):
		"""Verify all automation hook target paths resolve to callable Python functions."""
		paths = [h for _, _, h in _all_handlers() if "msuite_automation" in h]
		paths += [p for v in hooks.scheduler_events.values() for p in (v if isinstance(v, list) else [x for l in v.values() for x in l]) if "msuite_automation" in p]
		for p in paths:
			mod, fn = p.rsplit(".", 1)
			self.assertTrue(callable(getattr(importlib.import_module(mod), fn)), p)

	def test_shims(self):
		"""Verify backward-compatibility service shims remain callable."""
		from msuite_automation.msuite_automation.services import notification_service as ns
		for fn in ("dispatch_order_automation_workflow", "schedule_doc_event_notification", "run_server_script_for_doc_event"):
			self.assertTrue(callable(getattr(ns, fn)))
