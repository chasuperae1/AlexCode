"""Late-bound access to the ``alex.tui`` facade module.

The UI mixins and the startup path live below ``alex.tui`` in the import graph,
so they cannot import its symbols directly without creating a cycle. Resolving
the module at call time also keeps ``monkeypatch.setattr(alex.tui, ...)``
working for tests.
"""

from __future__ import annotations

from types import ModuleType


def _facade() -> ModuleType:
    """Return the ``alex.tui`` module, imported lazily on each call."""
    from alex import tui as facade

    return facade
