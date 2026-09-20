"""Cooperative deadlines at trusted data-tool boundaries, with lease checks."""
from contextlib import contextmanager
from contextvars import ContextVar
import threading
import time

_active = ContextVar("v3_tool_budget", default=None)


class ToolInterrupted(ValueError):
    """A tool must stop before its next chunk or publication boundary."""


class ToolBudget:
    def __init__(self, seconds, check_owner=None):
        self.deadline = time.monotonic() + seconds
        self.check_owner = check_owner
        self.next_check = 0
        self.lock = threading.Lock()

    def check(self):
        now = time.monotonic()
        if now >= self.deadline:
            raise ToolInterrupted("工具达到执行时限，已在分块边界停止；请缩小范围或调整方法")
        if self.check_owner and now >= self.next_check:
            with self.lock:
                if now >= self.next_check:
                    self.check_owner()
                    self.next_check = now + 1


def current_budget():
    return _active.get()


def checkpoint():
    budget = current_budget()
    if budget:
        budget.check()


@contextmanager
def tool_budget(seconds, check_owner=None):
    budget = ToolBudget(seconds, check_owner)
    token = _active.set(budget)
    try:
        budget.check()
        yield budget
        budget.check()
    finally:
        _active.reset(token)
