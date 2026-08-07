"""Root conftest — stubs the one driver tests can't install.

`asyncpg` is a C extension with no wheel for the newest CPython, so requiring it
would make `pytest` fail at collection on a dev machine. Every DB call under
test is mocked anyway, so a stub loses nothing. Same idea as pi_client's spidev
stub.

`aiohttp` is deliberately NOT stubbed even though little under test uses it
directly: aiogram imports it internally, and handing aiogram a MagicMock in
place of a real HTTP library breaks its own imports. It installs cleanly
everywhere, so it's a real dev dependency.

This lives at the repo root rather than in each tests/ directory because
`api/tests`, `bot/tests` and `consumer/tests` all import modules that
`import asyncpg` at module scope, and pytest loads the root conftest before any
of those packages is touched.
"""
import sys
from unittest.mock import MagicMock

_asyncpg = MagicMock()
# Real exception classes: these appear in `except (...)` tuples, where a
# MagicMock raises TypeError instead of matching.
_asyncpg.PostgresError = type("PostgresError", (Exception,), {})
_asyncpg.UniqueViolationError = type("UniqueViolationError", (_asyncpg.PostgresError,), {})
sys.modules.setdefault("asyncpg", _asyncpg)
