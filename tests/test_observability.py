from __future__ import annotations

from contextlib import contextmanager
from unittest.mock import Mock

from modal_devin import _observability as observability
from modal_devin._config import WorkerConfig


def test_logfire_configuration_is_safe_and_idempotent(monkeypatch):
    config = WorkerConfig("demo", "outpost_env-demo")
    configure = Mock()
    monkeypatch.setattr(observability, "_configured", False)
    monkeypatch.setattr(observability, "_service_version", Mock(return_value="1.2.3"))
    monkeypatch.setattr(observability.logfire, "configure", configure)

    observability.configure_observability(config)
    observability.configure_observability(config)

    configure.assert_called_once_with(
        send_to_logfire="if-token-present",
        service_name=config.app_name,
        service_version="1.2.3",
        console=False,
        distributed_tracing=True,
    )


def test_spans_have_worker_outpost_and_session_attributes(monkeypatch):
    config = WorkerConfig("demo", "outpost_env-demo")
    captured = Mock()

    @contextmanager
    def fake_span(name, **attributes):
        captured(name, attributes)
        yield

    monkeypatch.setattr(observability, "_configured", True)
    monkeypatch.setattr(observability.logfire, "span", fake_span)

    with observability.span(
        "modal-devin test",
        config=config,
        session_id="devin-1",
        attributes={"modal.function.name": "session"},
    ):
        pass

    captured.assert_called_once_with(
        "modal-devin test",
        {
            "_span_name": "modal-devin test",
            "modal_devin.worker.name": "demo",
            "modal_devin.outpost.id": "outpost_env-demo",
            "devin.session.id": "devin-1",
            "modal.function.name": "session",
        },
    )


def test_trace_context_uses_logfire_otel_carriers(monkeypatch):
    carrier = {"traceparent": "00-0123456789abcdef0123456789abcdef-0123456789abcdef-01"}
    monkeypatch.setattr(observability, "_configured", True)
    monkeypatch.setattr(observability.logfire, "get_context", Mock(return_value=carrier))

    attached = []

    @contextmanager
    def fake_attach(value):
        attached.append(value)
        yield

    monkeypatch.setattr(observability.logfire, "attach_context", fake_attach)

    assert observability.get_context() == carrier
    with observability.attach_context(carrier):
        pass

    assert attached == [carrier]
