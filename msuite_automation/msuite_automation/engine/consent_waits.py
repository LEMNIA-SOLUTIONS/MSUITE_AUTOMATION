"""Automations waiting on an Instagram consent reply. The Inbox tells this app when one changes."""

from __future__ import annotations

from msuite_core.constants import IG_CONSENT_DOCTYPE


def wake_waits(names: list[str]) -> None:
	"""Check the automations waiting on these Instagram Consent Response rows."""
	from msuite_automation.msuite_automation.constants import QUEUE_SHORT
	from msuite_automation.msuite_automation.engine import executor, triggers

	if IG_CONSENT_DOCTYPE not in triggers.get_watch_map():
		return

	for name in names:
		executor.enqueue_safely(
			triggers.CHECK_EVENT_WAITS_PATH,
			queue=QUEUE_SHORT,
			job_id=f"check_event_waits:{IG_CONSENT_DOCTYPE}:{name}",
			deduplicate=True,
			doctype=IG_CONSENT_DOCTYPE,
			name=name,
		)
