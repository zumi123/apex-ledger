from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ToolError(Exception):
    error_type: str
    message: str
    suggested_action: str | None = None
    stream_id: str | None = None
    expected_version: int | None = None
    actual_version: int | None = None

    def to_dict(self) -> dict:
        d = {
            "error_type": self.error_type,
            "message": self.message,
        }
        if self.suggested_action:
            d["suggested_action"] = self.suggested_action
        if self.stream_id:
            d["stream_id"] = self.stream_id
        if self.expected_version is not None:
            d["expected_version"] = self.expected_version
        if self.actual_version is not None:
            d["actual_version"] = self.actual_version
        return d

