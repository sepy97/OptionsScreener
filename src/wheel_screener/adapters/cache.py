"""CacheStore implementation shared by the CLI and the future API, so repeated runs
and the server hit cache rather than the vendors.

TODO(M2): hishel-backed (file/sqlite) storage.
"""

from __future__ import annotations
