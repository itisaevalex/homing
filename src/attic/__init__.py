"""attic — evict cold data to encrypted object storage.

Third sibling CLI alongside ``homing`` and ``cabinet``. attic frees local disk
by moving cold data to an rclone crypt (encrypted) remote — "upload once,
download once". It is EVICTION, not backup: the local copy is freed ONLY after
a real download → decrypt → re-hash round trip proves the remote copy is
independently recoverable under the currently-loaded crypt config.
"""

from __future__ import annotations

__version__ = "0.1.0"
