"""Scheduler wiring for the Instagram consent expiry job."""

import unittest

import frappe

EXPIRE_STALE_PATH = "msuite_inbox.inbox.services.ig_consent.expire_stale"
# Daily jobs that predate the consent feature; must survive any edit to hooks.
EXISTING_DAILY_JOBS = (
	"msuite_automation.msuite_automation.engine.triggers.schedule_daily",
	"msuite_automation.msuite_automation.engine.triggers.run_date_based",
	"msuite_commerce.meta_ecommerce.services.item_sync.push_sale_price_changes",
)


def _daily_paths():
	"""Return every dotted path scheduled as a daily job, across all installed apps."""
	return [
		p
		for entry in frappe.get_hooks("scheduler_events").get("daily", [])
		for p in (entry if isinstance(entry, list) else [entry])
	]


class TestIgConsentWiring(unittest.TestCase):
	"""Checks the consent expiry cron is registered and resolvable."""

	def test_expire_stale_is_scheduled(self):
		"""expire_stale is a daily job and its path resolves to a callable."""
		self.assertIn(EXPIRE_STALE_PATH, _daily_paths())
		self.assertTrue(callable(frappe.get_attr(EXPIRE_STALE_PATH)))

	def test_existing_daily_jobs_still_scheduled(self):
		"""Regression: adding the entry did not drop other daily jobs."""
		daily = _daily_paths()
		for path in EXISTING_DAILY_JOBS:
			self.assertIn(path, daily)
