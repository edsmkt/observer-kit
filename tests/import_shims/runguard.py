"""Compat import path: ``import runguard`` resolves to the package implementation.

Acceptance tests and legacy workflows use bare ``import runguard``. This shim
rebinds the module so private helpers (e.g. ``_lockfile``) match a vendored
copy of the package module.
"""
from observer_kit import runguard as _impl
import sys

sys.modules[__name__] = _impl
