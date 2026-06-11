from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass
class ProxyError(Exception):
    http_status: int
    code: str
    message: str
    details: Any = None

    def as_body(self, request_id: str) -> dict[str, Any]:
        body: dict[str, Any] = {
            "status": self.http_status,
            "code": self.code,
            "message": self.message,
            "request_id": request_id,
        }
        if self.details is not None:
            body["details"] = self.details
        return body
