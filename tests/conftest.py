"""Make the agent importable without a Technocore identity on the machine.

technocore_pulse imports technocore_agent at module load and exits if it is
missing, which is right for a run and wrong for a test: the measurement logic
has nothing to do with signing, and a test suite that needs a private key is a
test suite nobody runs. So a stub stands in for the signing module, and the
tests only touch the pure functions.
"""

from __future__ import annotations

import sys
import types
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent


def _install_stub() -> None:
    stub = types.ModuleType("technocore_agent")
    stub.DEFAULT_BASE_URL = "https://technocore.chat"
    stub.DEFAULT_TIMEOUT_SECONDS = 10.0
    stub.validate_base_url = lambda url: url.rstrip("/")
    stub.validate_name = lambda value, label="room": value
    stub.read_room = lambda *args, **kwargs: {"messages": []}
    stub.request_json = lambda *args, **kwargs: {}
    stub.post_signed_message = lambda *args, **kwargs: {"posted": {}}
    stub.load_identity = lambda *args, **kwargs: None
    stub.did_from_private_key = lambda key: "did:key:zStub"
    sys.modules.setdefault("technocore_agent", stub)


_install_stub()
sys.path.insert(0, str(REPO_ROOT))
