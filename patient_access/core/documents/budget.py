"""The document caps as a value: what has been spent, and whether the next document fits.

The only place caps are checked. Immutable: `spend` returns the next budget.
"""

from __future__ import annotations

from ..models import SkipReason, Value
from ..settings import DocumentCaps


class Budget(Value):
    caps: DocumentCaps
    count: int = 0
    bytes: int = 0

    def refusal(self, size: int | None = None) -> SkipReason | None:
        """Why a document of `size` bytes (unknown: None) cannot be stored, or None if it can."""
        if self.count >= self.caps.max_count:
            return SkipReason.COUNT_CAP
        if size is not None:
            if size > self.caps.max_bytes_each:
                return SkipReason.SIZE_CAP_EACH
            if self.bytes + size > self.caps.max_total_bytes:
                return SkipReason.SIZE_CAP_TOTAL
        return None

    @property
    def limit(self) -> int:
        """The most bytes the next document may have."""
        return max(0, min(self.caps.max_bytes_each, self.caps.max_total_bytes - self.bytes))

    def spend(self, size: int) -> Budget:
        return self.model_copy(update={"count": self.count + 1, "bytes": self.bytes + size})
