"""Messung je Tool-Aufruf (SPEC ss17.4).

Ohne Zahlen laesst sich Antwortoekonomie nicht beurteilen. Geloggt werden
Toolname, Dauer und Antwortgroesse - **keine** Parameterwerte, keine URL, kein
Pfad. Ein Shot-Filter kann harmlos aussehen, aber der Secret-Pfad steht in
derselben Anfrage, und Argumente sind der wahrscheinlichste Weg, auf dem
irgendwann etwas Vertrauliches in eine Logzeile geraet.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any

from fastmcp.server.middleware import CallNext, Middleware, MiddlewareContext

log = logging.getLogger(__name__)


def response_bytes(result: Any) -> int:
    """Groesse der Antwort in Byte, so wie sie auf die Leitung geht.

    Bevorzugt der strukturierte Teil - das ist das, was das Modell liest.
    Faellt er weg, zaehlen die Textbloecke.
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
    """Loggt je Tool-Aufruf ``tool``, ``dur_ms`` und ``bytes``."""

    async def on_call_tool(
        self, context: MiddlewareContext, call_next: CallNext
    ) -> Any:
        name = getattr(context.message, "name", "?")
        started = time.perf_counter()
        try:
            result = await call_next(context)
        except Exception as exc:
            # Nur der Fehlertyp - die Meldung koennte Argumente enthalten.
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
