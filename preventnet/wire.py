"""Wire-format allowlist and disclosure ledger.

strip_for_wire is the sole gate between a role's raw model answer and
anything a Coordinator phase ever sees. It is an allowlist, not a blocklist:
a field added to a record later cannot leak by being forgotten here.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from typing import Callable, Optional

ALLOWED_WIRE_FIELDS = {"field", "value", "band", "purpose", "source", "ts", "confidence"}
ALLOWED_VALUE_TYPES = (bool, str)
MAX_STRING_LEN = 40
# Defense in depth: these must never appear even though they're not in the
# allowlist above; a bug that widened ALLOWED_WIRE_FIELDS could not resurrect them.
BLOCKED_KEYS = {"record_id", "text", "note", "name", "dob"}


class WireViolation(Exception):
    """Raised when a raw dict cannot be safely reduced to wire format."""


@dataclass
class WireItem:
    field: str
    value: object
    purpose: str
    source: str
    ts: str
    band: Optional[str] = None
    confidence: Optional[str] = None
    id: Optional[str] = None  # assigned by Ledger.append, never by strip_for_wire

    def as_wire_dict(self) -> dict:
        d = {
            "id": self.id,
            "field": self.field,
            "value": self.value,
            "purpose": self.purpose,
            "source": self.source,
            "ts": self.ts,
        }
        if self.band is not None:
            d["band"] = self.band
        if self.confidence is not None:
            d["confidence"] = self.confidence
        return d


@dataclass
class Refusal:
    question_id: str
    role: str
    reason: str


def strip_for_wire(raw: dict) -> WireItem:
    blocked = BLOCKED_KEYS & raw.keys()
    if blocked:
        raise WireViolation(f"forbidden keys present: {sorted(blocked)}")

    unknown = raw.keys() - ALLOWED_WIRE_FIELDS
    if unknown:
        raise WireViolation(f"unknown keys not in wire allowlist: {sorted(unknown)}")

    for key, val in raw.items():
        if isinstance(val, str) and len(val) > MAX_STRING_LEN:
            raise WireViolation(f"value for '{key}' exceeds {MAX_STRING_LEN} chars")

    if "value" in raw and not isinstance(raw["value"], ALLOWED_VALUE_TYPES):
        raise WireViolation(f"'value' must be bool or str, got {type(raw['value']).__name__}")

    for required in ("field", "value", "purpose", "source", "ts"):
        if required not in raw:
            raise WireViolation(f"missing required wire field: {required}")

    return WireItem(
        field=raw["field"],
        value=raw["value"],
        purpose=raw["purpose"],
        source=raw["source"],
        ts=raw["ts"],
        band=raw.get("band"),
        confidence=raw.get("confidence"),
    )


class Ledger:
    """Append-only disclosure ledger. Every append/refuse also emits a live event."""

    def __init__(self, emit: Callable[[dict], None]):
        self._emit = emit
        self.items: list[WireItem] = []
        self.refusals: list[Refusal] = []
        self._counter = 0

    def append(self, item: WireItem) -> WireItem:
        self._counter += 1
        item.id = f"L{self._counter}"
        self.items.append(item)
        self._emit(
            {
                "type": "preventnet.disclosure",
                "preventnet": "disclosure",
                "role": item.source,
                "field": item.field,
                "band": item.band,
                "purpose": item.purpose,
            }
        )
        return item

    def refuse(self, refusal: Refusal) -> None:
        self.refusals.append(refusal)
        self._emit(
            {
                "type": "preventnet.refusal",
                "preventnet": "refusal",
                "role": refusal.role,
                "question_id": refusal.question_id,
                "reason": refusal.reason,
            }
        )

    def to_json(self) -> str:
        return json.dumps(
            {
                "ledger": [item.as_wire_dict() for item in self.items],
                "refusals": [asdict(r) for r in self.refusals],
            }
        )
