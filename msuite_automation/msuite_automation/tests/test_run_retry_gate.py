"""A retried run must not count toward the monthly automation-run plan gate (no live calls)."""

import unittest
from types import SimpleNamespace

from msuite_automation.msuite_automation.plan_gates import GATES


class TestRunRetryGate(unittest.TestCase):
	def _doc(self, **flags):
		return SimpleNamespace(flags=SimpleNamespace(**flags), name=None, is_new=lambda: True)

	def test_retry_run_skips_the_gate(self):
		self.assertTrue(GATES["Messaging Automation Run"].skip_if(self._doc(is_retry=True)))

	def test_normal_new_run_is_gated(self):
		self.assertFalse(GATES["Messaging Automation Run"].skip_if(self._doc()))
