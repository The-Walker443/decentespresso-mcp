"""Measurement per tool call (SPEC §9.2).

Response economy cannot be judged without numbers. What gets logged is the
tool name, the duration and the response size - **no** parameter values, no
URL, no path. A shot filter may look harmless, but the secret path sits in
the same request, and arguments are the likeliest route by which something
confidential eventually ends up in a log line.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

log = logging.getLogger(__name__)


def response_bytes(result: Any) -> int:
    """Size of the response in bytes, as it goes over the wire.

    The structured part is preferred - that is what the model reads. If it is
    absent, the text blocks are counted.
    """
    structured = getattr(result, "structured_content", None)
    if structured is not None:
        try:
            return len(json.dumps(structured, ensure_ascii=False, default=str)
                       .encode("utf-8"))
        except (TypeError, ValueError):
            pass
    total = 0
    for block in getattr(result, "content", None) or []:
        text = getattr(block, "text", None)
        if text:
            total += len(text.encode("utf-8"))
    return total


class CallMetricsMiddleware(Middleware):
    """Logs ``tool``, ``dur_ms`` and ``bytes`` for every tool call."""

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        name = getattr(context.message, "name", "?")
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            # The error type only - the message could carry arguments.
            log.info(
                "tool call failed",
                extra={"fields": {
                    "tool": name,
                    "dur_ms": round((time.perf_counter() - started) * 1000, 1),
                    "error": type(exc).__name__,
                }},
            )
            raise

        log.info(
            "tool call",
            extra={"fields": {
                "tool": name,
                "dur_ms": round((time.perf_counter() - started) * 1000, 1),
                "bytes": response_bytes(result),
            }},
        )
        return result
