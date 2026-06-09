"""OpenTelemetry 셋업 — `gen_ai.*` 시맨틱 컨벤션 사용 (설계 §3.4).

스팬 속성:
  - gen_ai.system          : "anthropic" | "openai" | "openai-compatible"
  - gen_ai.request.model   : 모델 명
  - gen_ai.usage.input_tokens / output_tokens

FastAPI 인스트루멘테이션 + OTLP gRPC exporter.
exporter_endpoint가 빈 문자열이면 콘솔 exporter로 폴백(로컬 개발 편의).
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from opentelemetry.trace import Tracer

logger = logging.getLogger(__name__)


def setup_otel(
    app: FastAPI,
    *,
    service_name: str = "ccim-gateway",
    exporter_endpoint: str = "",
) -> None:
    """FastAPI 인스트루멘테이션 + OTLP exporter 등록.

    호출 시점: `create_app()` 내부 또는 lifespan.
    실패해도 게이트웨이 기동을 막지 않는다 (경고 로그만).
    """
    try:
        _setup(app, service_name=service_name, exporter_endpoint=exporter_endpoint)
    except Exception as exc:
        logger.warning(
            "OpenTelemetry setup failed (tracing disabled): %s", exc, exc_info=True
        )


def _setup(app: FastAPI, *, service_name: str, exporter_endpoint: str) -> None:
    from opentelemetry import trace
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor, ConsoleSpanExporter

    resource = Resource.create({"service.name": service_name})
    provider = TracerProvider(resource=resource)

    if exporter_endpoint:
        try:
            from opentelemetry.exporter.otlp.proto.grpc.trace_exporter import (
                OTLPSpanExporter,
            )

            exporter = OTLPSpanExporter(endpoint=exporter_endpoint, insecure=True)
        except ImportError:
            logger.warning(
                "opentelemetry-exporter-otlp-proto-grpc not installed; "
                "falling back to ConsoleSpanExporter"
            )
            exporter = ConsoleSpanExporter()  # type: ignore[assignment]
    else:
        exporter = ConsoleSpanExporter()  # type: ignore[assignment]

    provider.add_span_processor(BatchSpanProcessor(exporter))
    trace.set_tracer_provider(provider)

    # FastAPI / ASGI 자동 인스트루멘테이션
    try:
        from opentelemetry.instrumentation.fastapi import FastAPIInstrumentor

        FastAPIInstrumentor.instrument_app(
            app,
            tracer_provider=provider,
            excluded_urls="/health",
        )
    except ImportError:
        logger.warning(
            "opentelemetry-instrumentation-fastapi not installed; "
            "HTTP span instrumentation skipped"
        )

    logger.info(
        "OpenTelemetry configured: service=%s endpoint=%s",
        service_name,
        exporter_endpoint or "(console)",
    )


def get_tracer(name: str = "ccim") -> Tracer:
    """모듈에서 직접 tracer를 얻는 헬퍼."""
    from opentelemetry import trace

    return trace.get_tracer(name)
