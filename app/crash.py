"""Crash injection for the crash demo and the tests. Never used in normal operation.

The runner and worker take an `on_step` hook that is called after every tool call (supervisor's and
specialists'). A tool's side effect has already been committed to pitlane.db by the time its step event
fires, so a hook that dies right after a side-effect tool reproduces the nasty case: the world has changed,
but agent.db does not yet know. `crash_after` builds that hook without touching the real code path.
"""
from collections.abc import Callable


class SimulatedCrash(BaseException):
    """Stands in for the process dying. A BaseException on purpose: the worker's `except Exception`
    handlers must not swallow it, exactly as they could not swallow a real kill -9."""


def crash_after(agent: str, tool: str, action: Callable[[], None]) -> Callable[[dict], None]:
    """on_step hook: call `action()` once, right after `agent` has finished a fresh (not replayed) `tool` call."""
    fired = []

    def hook(event: dict) -> None:
        if (not fired and event.get("kind") == "tool" and event.get("agent") == agent
                and event.get("tool") == tool and not event.get("replayed")):
            fired.append(True)
            action()

    return hook
