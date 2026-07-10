from __future__ import annotations

import json
import urllib.error
from email.message import Message

import pytest

from modal_devin import OutpostsAPIError, OutpostsProtocolError
from modal_devin._client import ClaimConflict, OutpostsClient


class Response:
    def __init__(self, body):
        self.body = body

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        return None

    def read(self, size=-1):
        return self.body if size < 0 else self.body[:size]


class RecordingUrlOpen:
    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.requests = []
        self.timeouts = []

    def __call__(self, request, *, timeout):
        self.requests.append(request)
        self.timeouts.append(timeout)
        body = self.bodies.pop(0)
        if isinstance(body, BaseException):
            raise body
        return Response(json.dumps(body).encode())


def make_client(recorder):
    return OutpostsClient(
        "https://api.example.com/",
        "token-123",
        timeout=7.5,
        urlopen=recorder,
    )


def test_pending_sessions_are_typed_and_requests_have_standard_headers():
    recorder = RecordingUrlOpen(
        [{"items": [{"metadata": {"session_id": "devin-1"}}, {"bad": "item"}]}]
    )

    result = make_client(recorder).pending_session_ids("pool with spaces")

    assert result == ("devin-1",)
    [request] = recorder.requests
    assert request.full_url.endswith("pool=pool+with+spaces&phase=pending")
    assert request.get_header("Authorization") == "Bearer token-123"
    assert request.get_header("Accept") == "application/json"
    assert request.get_header("User-agent") == "modal-devin"
    assert recorder.timeouts == [7.5]


def test_claim_quotes_session_id_and_parses_deadline():
    recorder = RecordingUrlOpen([{"status": {"claim_deadline": "tomorrow"}}])

    claim = make_client(recorder).claim("session/1", "modal-worker")

    assert claim.deadline == "tomorrow"
    [request] = recorder.requests
    assert "/session%2F1/claim" in request.full_url
    assert json.loads(request.data) == {"acceptor_id": "modal-worker"}


def test_claim_conflict_has_a_domain_exception():
    error = urllib.error.HTTPError("url", 409, "Conflict", Message(), None)
    recorder = RecordingUrlOpen([error])

    with pytest.raises(ClaimConflict):
        make_client(recorder).claim("devin-1", "modal-worker")


def test_http_and_transport_failures_have_domain_exceptions():
    server_error = urllib.error.HTTPError("url", 500, "Nope", Message(), None)
    with pytest.raises(OutpostsAPIError):
        make_client(RecordingUrlOpen([server_error])).pending_session_ids("pool")

    with pytest.raises(OutpostsAPIError):
        make_client(RecordingUrlOpen([urllib.error.URLError("offline")])).pending_session_ids(
            "pool"
        )


@pytest.mark.parametrize("body", [[], {"not_items": []}])
def test_malformed_responses_raise_protocol_errors(body):
    recorder = RecordingUrlOpen([body])

    with pytest.raises(OutpostsProtocolError):
        make_client(recorder).pending_session_ids("pool")


def test_status_distinguishes_successful_absence_from_request_failure():
    client = make_client(RecordingUrlOpen([{"items": []}]))
    assert client.session_status("devin-1", "modal-worker") is None

    failing = make_client(RecordingUrlOpen([urllib.error.URLError("offline")]))
    with pytest.raises(OutpostsAPIError):
        failing.session_status("devin-1", "modal-worker")


def test_response_size_is_bounded():
    def oversized(request, *, timeout):
        return Response(b"{" + b"x" * (1024 * 1024 + 1))

    client = OutpostsClient(
        "https://api.example.com",
        "token",
        timeout=1,
        urlopen=oversized,
    )

    with pytest.raises(OutpostsProtocolError, match="exceeded 1 MiB"):
        client.pending_session_ids("pool")
