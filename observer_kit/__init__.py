"""Observer Kit — package-owned runtime for supervised agent data workflows.

Import the observed-run API from this package (not from a skill directory):

    from observer_kit.runguard import start_observed_run, ledger

CLI entry: ``observer-kit`` or ``python -m observer_kit``.
"""

from __future__ import annotations

__all__ = ["__version__", "runguard"]
__version__ = "0.2.0"

# Re-export the runguard module for ``from observer_kit import runguard``.
from observer_kit import runguard as runguard  # noqa: E402
