"""Root conftest — runtime drivers that tests never actually exercise.

`asyncpg` and `aiohttp` are stubbed in `sys.modules` before any service package
is imported. Every DB call and every outbound HTTP call under test is mocked, so
the real drivers add nothing but install friction: asyncpg is a C extension with
no wheel for the newest CPython, which would make `pytest` fail at collection on
a dev machine. Same idea as pi_client's spidev stub.

This lives at the repo root rather than in each tests/ directory because both
`api/tests` and `consumer/tests` import modules that `import asyncpg` at module
scope, and pytest loads the root conftest before either package is touched.
"""
import sys
from unittest.mock import MagicMock

_asyncpg = MagicMock()
# Real exception classes: these appear in `except (...)` tuples, where a
# MagicMock raises TypeError instead of matching.
_asyncpg.PostgresError = type("PostgresError", (Exception,), {})
_asyncpg.UniqueViolationError = type("UniqueViolationError", (_asyncpg.PostgresError,), {})
sys.modules.setdefault("asyncpg", _asyncpg)

_aiohttp = MagicMock()
_aiohttp.ClientError = type("ClientError", (Exception,), {})
sys.modules.setdefault("aiohttp", _aiohttp)
