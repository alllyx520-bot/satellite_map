import time
from unittest.mock import patch

from django.test import SimpleTestCase

from .v3.runtime import ToolInterrupted, checkpoint, current_budget, tool_budget


class ToolDeadlineTests(SimpleTestCase):
    def test_timeout_stops_work_at_next_chunk_and_clears_context(self):
        written = []
        with self.assertRaises(ToolInterrupted):
            with tool_budget(.01):
                written.append("first chunk")
                time.sleep(.02)
                checkpoint()
                written.append("late chunk")
        self.assertEqual(written, ["first chunk"])
        self.assertIsNone(current_budget())

    def test_lease_loss_stops_before_publication(self):
        owner = [True]
        def check_owner():
            if not owner[0]:
                raise ToolInterrupted("lost ownership")
        with self.assertRaises(ToolInterrupted):
            with tool_budget(10, check_owner) as budget:
                owner[0] = False
                budget.next_check = 0
                checkpoint()
        self.assertIsNone(current_budget())

    def test_nested_budgets_restore_parent(self):
        with tool_budget(10) as outer:
            with tool_budget(5):
                self.assertIsNot(current_budget(), outer)
            self.assertIs(current_budget(), outer)
