"""Logging, and traces to Application Insights when the container has a connection string.

Agent Framework emits its own `invoke_agent`, `chat` and `execute_tool` spans once a tracer
provider exists; this module only adds the parent `pundit.turn` span and the speech spans.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

from opentelemetry import trace
from opentelemetry.trace import Span, Status, StatusCode

tracer = trace.get_tracer("second_screen_pundit")
logger = logging.getLogger(__name__)
_configured = False


def configure_observability() -> bool:
    """Configure logging, then Azure Monitor if APPLICATIONINSIGHTS_CONNECTION_STRING is set.

    Ingestion is keyless: Application Insights has local auth disabled, and telemetry is sent
    with the app's managed identity (AZURE_CLIENT_ID), which holds Monitoring Metrics Publisher.
    Returns whether Azure Monitor was configured.
    """
    global _configured
    if _configured:
        return True
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    # The Azure SDK logs every HTTP request, telemetry exports included, at INFO.
    logging.getLogger("azure").setLevel(logging.WARNING)
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "").strip()
    if not connection_string:
        return False
    from azure.identity import DefaultAzureCredential, ManagedIdentityCredential
    from azure.monitor.opentelemetry import configure_azure_monitor

    client_id = os.getenv("AZURE_CLIENT_ID", "").strip()
    credential = (
        ManagedIdentityCredential(client_id=client_id) if client_id else DefaultAzureCredential()
    )
    configure_azure_monitor(
        connection_string=connection_string,
        credential=credential,
        # App logs (warm-up, agent_call timings, guard trips) go to App Insights as traces.
        logger_name="second_screen_pundit",
        enable_live_metrics=False,
    )
    _configured = True
    logging.getLogger(__name__).info("azure_monitor_configured")
    return True


def _evidence_status(result: Any) -> str:
    evidence = getattr(result, "evidence", None)
    if evidence is None:
        return "none"
    return "passed" if evidence.passed else "failed"


def record_result(span: Span, result: Any) -> None:
    """Attributes known only after the agent spans close: evidence, source and tokens."""
    if result is None:
        span.set_attribute("app.evidence.status", "none")
        return
    span.set_attribute("app.evidence.status", _evidence_status(result))
    status = getattr(result, "status", None) or getattr(result, "source", None)
    if status:
        span.set_attribute("app.outcome", str(status))
    timings = getattr(result, "timings", ()) or ()
    input_tokens = sum(t.input_tokens or 0 for t in timings)
    output_tokens = sum(t.output_tokens or 0 for t in timings)
    span.set_attribute("app.agent_calls", len(timings))
    span.set_attribute("app.input_tokens", input_tokens)
    span.set_attribute("app.output_tokens", output_tokens)
    if timings:
        attributes = getattr(span, "attributes", None) or {}
        logger.info(
            "turn_tokens route=%s match_id=%s calls=%d input_tokens=%d output_tokens=%d",
            attributes.get("app.route", "-"),
            attributes.get("app.match_id", "-"),
            len(timings),
            input_tokens,
            output_tokens,
        )


@contextmanager
def turn_span(route: str, match_id: str) -> Iterator[Span]:
    """The parent span of one pipeline run: `live`, `question` or `recap`."""
    with tracer.start_as_current_span("pundit.turn") as span:
        span.set_attribute("app.route", route)
        span.set_attribute("app.match_id", match_id)
        try:
            yield span
        except Exception as exc:
            span.set_status(Status(StatusCode.ERROR, type(exc).__name__))
            raise
