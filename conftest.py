"""pytest setup shared by all tests.

- Makes `import outbound` work when you run `pytest` from the project folder.
- Blocks ALL internet access during tests, so a test can never spend an API credit by accident.
  (Connections to 127.0.0.1 are allowed: Windows uses one internally for the test web server.)
"""

import socket
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

_real_connect = socket.socket.connect
LOCAL_HOSTS = {"127.0.0.1", "::1", "localhost"}


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    def guarded_connect(self, address, *args, **kwargs):
        host = address[0] if isinstance(address, tuple) else address
        if host in LOCAL_HOSTS:
            return _real_connect(self, address, *args, **kwargs)
        raise RuntimeError("Network access is disabled in tests (use fixtures instead).")

    def blocked(*args, **kwargs):
        raise RuntimeError("Network access is disabled in tests (use fixtures instead).")

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket, "create_connection", blocked)
