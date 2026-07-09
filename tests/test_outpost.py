"""Tests for the network-free logic in modal_devin.outpost: the HTTP client, the poll/claim/
dispatch loop, private-repo cloning, and image construction.

Sandbox/sidecar execution and real Modal image builds (`worker_image(...).build(...)`,
`build_sidecar_image_id`, full `run_session` execution) talk to the live Modal API and aren't
covered here -- they're exercised by hand against a real workspace instead.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import urllib.error
import urllib.request
from email.message import Message
from types import SimpleNamespace
from typing import cast
from unittest.mock import Mock, call

import modal
import pytest

from modal_devin import outpost


def _secret_ref(name: str | None) -> modal.Secret:
    return cast(modal.Secret, SimpleNamespace(name=name))


# ------------------------------------------------------------------------------------------------
# _api_request
# ------------------------------------------------------------------------------------------------


class _RecordingUrlopen:
    """Stand-in for `urllib.request.urlopen`: records every `Request` it's called with and
    answers with a canned JSON body, so tests can inspect exactly what `_api_request` sent."""

    def __init__(self):
        self.response_body = {}
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req, **kwargs):
        self.requests.append(req)
        return io.BytesIO(json.dumps(self.response_body).encode())


@pytest.fixture
def fake_urlopen(monkeypatch):
    recorder = _RecordingUrlopen()
    monkeypatch.setattr(outpost.urllib.request, "urlopen", recorder)
    return recorder


class TestApiRequest:
    def test_get_sends_bearer_auth_and_no_body(self, fake_urlopen):
        fake_urlopen.response_body = {"items": []}

        result = outpost._api_request("https://api.example.com", "tok-123", "GET", "/pending")

        assert result == {"items": []}
        [req] = fake_urlopen.requests
        assert req.full_url == "https://api.example.com/pending"
        assert req.get_method() == "GET"
        assert req.get_header("Authorization") == "Bearer tok-123"
        assert req.data is None

    def test_post_sends_json_body_with_content_type(self, fake_urlopen):
        outpost._api_request(
            "https://api.example.com", "tok", "POST", "/claim", {"acceptor_id": "modal-x"}
        )

        [req] = fake_urlopen.requests
        assert req.get_method() == "POST"
        assert json.loads(req.data) == {"acceptor_id": "modal-x"}
        assert req.get_header("Content-type") == "application/json"


class TestRelease:
    def test_posts_acceptor_id_to_the_release_endpoint(self, monkeypatch):
        release_request = Mock()
        monkeypatch.setattr(outpost, "_api_request", release_request)

        outpost._release("https://api.example.com", "tok", "sess-1", "modal-mypool")

        release_request.assert_called_once_with(
            "https://api.example.com",
            "tok",
            "POST",
            "/opbeta/outposts/devins/sess-1/release",
            {"acceptor_id": "modal-mypool"},
        )


# ------------------------------------------------------------------------------------------------
# poll_and_dispatch
# ------------------------------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def devin_token_env(monkeypatch):
    """poll_and_dispatch reads the Outposts token from the environment, same as the Modal Secret
    injects it at runtime."""
    monkeypatch.setenv("DEVIN_OUTPOSTS_TOKEN", "test-token")
    monkeypatch.delenv("DEVIN_API_URL", raising=False)


def _pending(*session_ids):
    return {"items": [{"metadata": {"session_id": sid}} for sid in session_ids]}


class TestPollAndDispatch:
    def test_accepts_a_pool_config_object(self, monkeypatch):
        api_calls = []

        def fake_api_request(api_url, token, method, path, body=None):
            api_calls.append((api_url, method, path, body))
            return _pending("sess-1") if method == "GET" else {"status": {}}

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            config=outpost.OutpostPoolConfig(
                name="mypool",
                pool_id="outpost_env-abc",
                api_url="https://api.example.com",
            ),
            run_session_fn=run_session_fn,
            sidecar_image_id="im-sidecar",
        )

        assert api_calls[0] == (
            "https://api.example.com",
            "GET",
            "/opbeta/outposts/devins?pool=outpost_env-abc&phase=pending",
            None,
        )
        assert api_calls[1][3] == {"acceptor_id": "modal-mypool"}
        run_session_fn.spawn.assert_called_once_with("sess-1", sidecar_image_id="im-sidecar")

    def test_dispatches_every_pending_session(self, monkeypatch):
        claimed_paths = []

        def fake_api_request(api_url, token, method, path, body=None):
            if method == "GET":
                return _pending("sess-1", "sess-2")
            claimed_paths.append(path)
            return {"status": {"claim_deadline": "2026-07-07T00:00:00Z"}}

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool",
            pool_id="outpost_env-abc",
            api_url="https://api.example.com",
            run_session_fn=run_session_fn,
            sidecar_image_id="im-sidecar",
        )

        run_session_fn.spawn.assert_has_calls(
            [
                call("sess-1", sidecar_image_id="im-sidecar"),
                call("sess-2", sidecar_image_id="im-sidecar"),
            ]
        )
        assert claimed_paths == [
            "/opbeta/outposts/devins/sess-1/claim",
            "/opbeta/outposts/devins/sess-2/claim",
        ]

    def test_skips_a_session_claimed_by_another_worker(self, monkeypatch):
        def fake_api_request(api_url, token, method, path, body=None):
            if method == "GET":
                return _pending("sess-1", "sess-2")
            if "sess-1" in path:
                raise urllib.error.HTTPError(path, 409, "Conflict", hdrs=Message(), fp=None)
            return {"status": {}}

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool",
            pool_id="outpost_env-abc",
            api_url="https://api.example.com",
            run_session_fn=run_session_fn,
            sidecar_image_id="im-sidecar",
        )

        run_session_fn.spawn.assert_called_once_with("sess-2", sidecar_image_id="im-sidecar")

    def test_keeps_going_after_an_unexpected_claim_error(self, monkeypatch):
        def fake_api_request(api_url, token, method, path, body=None):
            if method == "GET":
                return _pending("sess-1", "sess-2")
            if "sess-1" in path:
                raise urllib.error.HTTPError(path, 500, "Server Error", hdrs=Message(), fp=None)
            return {"status": {}}

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool",
            pool_id="outpost_env-abc",
            api_url="https://api.example.com",
            run_session_fn=run_session_fn,
            sidecar_image_id="im-sidecar",
        )

        run_session_fn.spawn.assert_called_once_with("sess-2", sidecar_image_id="im-sidecar")

    def test_keeps_going_after_a_malformed_claim_response(self, monkeypatch):
        def fake_api_request(api_url, token, method, path, body=None):
            if method == "GET":
                return _pending("sess-1", "sess-2")
            if "sess-1" in path:
                raise ValueError("not a JSON object")
            return {"status": {}}

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool",
            pool_id="outpost_env-abc",
            api_url="https://api.example.com",
            run_session_fn=run_session_fn,
            sidecar_image_id="im-sidecar",
        )

        run_session_fn.spawn.assert_called_once_with("sess-2", sidecar_image_id="im-sidecar")

    def test_returns_quietly_when_polling_itself_fails(self, monkeypatch):
        def fake_api_request(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool",
            pool_id="outpost_env-abc",
            api_url="https://api.example.com",
            run_session_fn=run_session_fn,
        )

        run_session_fn.spawn.assert_not_called()

    def test_returns_quietly_when_polling_returns_malformed_json(self, monkeypatch):
        def fake_api_request(*args, **kwargs):
            raise ValueError("not a JSON object")

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool",
            pool_id="outpost_env-abc",
            api_url="https://api.example.com",
            run_session_fn=run_session_fn,
        )

        run_session_fn.spawn.assert_not_called()

    def test_devin_api_url_env_overrides_the_configured_api_url(self, monkeypatch):
        api_urls_used = []

        def fake_api_request(api_url, token, method, path, body=None):
            api_urls_used.append(api_url)
            return _pending() if method == "GET" else {"status": {}}

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        monkeypatch.setenv("DEVIN_API_URL", "http://caddy:8686")

        outpost.poll_and_dispatch(
            pool_name="mypool",
            pool_id="outpost_env-abc",
            api_url="https://api.example.com",
            run_session_fn=Mock(),
        )

        assert api_urls_used == ["http://caddy:8686"]

    def test_builds_the_sidecar_before_claiming_when_no_id_is_supplied(self, monkeypatch):
        events = []

        def fake_api_request(api_url, token, method, path, body=None):
            events.append((method, path))
            return _pending("sess-1") if method == "GET" else {"status": {}}

        def fake_build_sidecar_image_id(pool_name):
            events.append(("BUILD", pool_name))
            return "im-built-sidecar"

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        monkeypatch.setattr(outpost, "build_sidecar_image_id", fake_build_sidecar_image_id)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool",
            pool_id="outpost_env-abc",
            api_url="https://api.example.com",
            run_session_fn=run_session_fn,
        )

        assert events == [
            ("GET", "/opbeta/outposts/devins?pool=outpost_env-abc&phase=pending"),
            ("BUILD", "mypool"),
            ("POST", "/opbeta/outposts/devins/sess-1/claim"),
        ]
        run_session_fn.spawn.assert_called_once_with(
            "sess-1", sidecar_image_id="im-built-sidecar"
        )

    def test_does_not_claim_when_sidecar_image_resolution_fails(self, monkeypatch):
        api_calls = []

        def fake_api_request(api_url, token, method, path, body=None):
            api_calls.append((method, path))
            return _pending("sess-1")

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        monkeypatch.setattr(
            outpost,
            "build_sidecar_image_id",
            Mock(side_effect=RuntimeError("modal unavailable")),
        )
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool",
            pool_id="outpost_env-abc",
            api_url="https://api.example.com",
            run_session_fn=run_session_fn,
        )

        assert api_calls == [("GET", "/opbeta/outposts/devins?pool=outpost_env-abc&phase=pending")]
        run_session_fn.spawn.assert_not_called()

    def test_releases_the_claim_when_dispatch_spawn_fails(self, monkeypatch):
        def fake_api_request(api_url, token, method, path, body=None):
            return _pending("sess-1") if method == "GET" else {"status": {}}

        release = Mock()
        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        monkeypatch.setattr(outpost, "_release", release)
        run_session_fn = Mock()
        run_session_fn.spawn.side_effect = RuntimeError("modal spawn failed")

        outpost.poll_and_dispatch(
            pool_name="mypool",
            pool_id="outpost_env-abc",
            api_url="https://api.example.com",
            run_session_fn=run_session_fn,
            sidecar_image_id="im-sidecar",
        )

        release.assert_called_once_with(
            "https://api.example.com", "test-token", "sess-1", "modal-mypool"
        )


# ------------------------------------------------------------------------------------------------
# clone_private_repo -- validation and wiring
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def secret():
    return _secret_ref("github-clone-token")


@pytest.fixture
def fake_image():
    image = Mock()
    image.run_commands.return_value = Mock()
    return image


class TestClonePrivateRepoValidation:
    @pytest.mark.parametrize("bad_repo_url", ["github.com/org/repo", "not-a-url", ""])
    def test_rejects_a_repo_url_without_a_scheme(self, fake_image, secret, bad_repo_url):
        with pytest.raises(ValueError, match="repo_url must be"):
            outpost.clone_private_repo(
                fake_image, bad_repo_url, "/root/workspace/x", token_secret=secret
            )

    @pytest.mark.parametrize(
        "bad_repo_url",
        [
            "http://github.com/org/repo",
            "ssh://github.com/org/repo",
            "file:///tmp/repo",
            "https://user:token@github.com/org/repo",
            "https://github.com/org/repo#main",
        ],
    )
    def test_rejects_repo_urls_that_would_not_be_scrubbed_cleanly(
        self, fake_image, secret, bad_repo_url
    ):
        with pytest.raises(ValueError):
            outpost.clone_private_repo(
                fake_image, bad_repo_url, "/root/workspace/x", token_secret=secret
            )

    @pytest.mark.parametrize("bad_env_var", ["123TOKEN", "GIT TOKEN", "", "GIT-TOKEN", "TØKEN"])
    def test_rejects_a_token_env_var_that_is_not_a_shell_identifier(
        self, fake_image, secret, bad_env_var
    ):
        with pytest.raises(ValueError, match="valid shell identifier"):
            outpost.clone_private_repo(
                fake_image,
                "https://github.com/org/repo",
                "/root/workspace/x",
                token_secret=secret,
                token_env_var=bad_env_var,
            )


class TestClonePrivateRepoWiring:
    def test_passes_the_secret_through_to_run_commands(self, fake_image, secret):
        outpost.clone_private_repo(
            fake_image,
            "https://github.com/org/repo",
            "/root/workspace/repo",
            token_secret=secret,
        )

        assert fake_image.run_commands.call_args.kwargs["secrets"] == [secret]

    def test_returns_the_image_that_run_commands_produces(self, fake_image, secret):
        result = outpost.clone_private_repo(
            fake_image,
            "https://github.com/org/repo",
            "/root/workspace/repo",
            token_secret=secret,
        )

        assert result is fake_image.run_commands.return_value


# ------------------------------------------------------------------------------------------------
# clone_private_repo -- the shell command it builds, run for real through `sh`
#
# `run_commands`'s argument is a shell script that will execute verbatim during the image build.
# Running the command for real through `sh`, with a fake `git` on PATH, proves the shell script
# invokes git with a plain repo URL and leaves the secret in GIT_ASKPASS instead of argv/loggable
# URLs.
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_git(tmp_path):
    """A `git` stub that appends its argv (one arg per line, invocations separated by `---`) to
    a log file instead of touching the network, so cloning is instant and offline."""
    log_path = tmp_path / "git-invocations.log"
    git_path = tmp_path / "git"
    git_path.write_text(
        "#!/bin/sh\n"
        'for a in "$@"; do printf \'%s\\n\' "$a" >> "$GIT_LOG"; done\n'
        "printf -- '---\\n' >> \"$GIT_LOG\"\n"
    )
    git_path.chmod(0o755)
    return log_path


def _run_shell(command, tmp_path, fake_git, env_overrides):
    env = dict(os.environ)
    env.pop("GIT_CLONE_TOKEN", None)  # never inherit one from the test runner's own shell
    env["PATH"] = f"{tmp_path}{os.pathsep}{env['PATH']}"
    env["GIT_LOG"] = str(fake_git)
    env.update(env_overrides)
    return subprocess.run(["sh", "-c", command], env=env, capture_output=True, text=True)


def _git_invocations(fake_git):
    if not fake_git.exists():
        return []
    blocks = fake_git.read_text().split("---\n")
    return [block.splitlines() for block in blocks if block.strip()]


class TestClonePrivateRepoShellCommand:
    def test_clones_with_plain_url_and_resets_the_remote(self, tmp_path, fake_git):
        image = Mock()
        outpost.clone_private_repo(
            image,
            "https://github.com/acme/widgets",
            "/root/workspace/widgets",
            token_secret=_secret_ref("github-clone-token"),
        )
        [command] = image.run_commands.call_args.args

        result = _run_shell(command, tmp_path, fake_git, {"GIT_CLONE_TOKEN": "s3cr3t-value"})

        assert result.returncode == 0, result.stderr
        assert "GIT_ASKPASS" in command
        assert "x-access-token:s3cr3t-value" not in command
        clone_argv, remote_argv = _git_invocations(fake_git)
        assert clone_argv == [
            "clone",
            "https://github.com/acme/widgets",
            "/root/workspace/widgets",
        ]
        assert remote_argv == [
            "-C",
            "/root/workspace/widgets",
            "remote",
            "set-url",
            "origin",
            "https://github.com/acme/widgets",
        ]

    def test_never_writes_the_secret_to_the_cloned_remote_url(self, tmp_path, fake_git):
        """Belt-and-suspenders: even though `fake_git` never creates a real .git/config, assert
        the final `remote set-url` call -- the one whose effect actually lands in the built image
        -- only ever sees the plain repo_url, never the credentialed one."""
        image = Mock()
        outpost.clone_private_repo(
            image,
            "https://github.com/acme/widgets",
            "/root/workspace/widgets",
            token_secret=_secret_ref("github-clone-token"),
        )
        [command] = image.run_commands.call_args.args

        _run_shell(command, tmp_path, fake_git, {"GIT_CLONE_TOKEN": "s3cr3t-value"})

        _clone_argv, remote_argv = _git_invocations(fake_git)
        assert not any("s3cr3t-value" in arg for arg in _clone_argv)
        assert not any("s3cr3t-value" in arg for arg in remote_argv)

    def test_fails_fast_with_a_clear_message_when_the_token_is_unset(self, tmp_path, fake_git):
        image = Mock()
        outpost.clone_private_repo(
            image,
            "https://github.com/acme/widgets",
            "/root/workspace/widgets",
            token_secret=_secret_ref("github-clone-token"),
        )
        [command] = image.run_commands.call_args.args

        result = _run_shell(command, tmp_path, fake_git, {})

        assert result.returncode != 0
        assert "GIT_CLONE_TOKEN is empty" in result.stderr
        assert "github-clone-token" in result.stderr
        assert _git_invocations(fake_git) == []  # git must never even be invoked

    def test_error_message_falls_back_to_a_placeholder_for_an_unnamed_secret(
        self, tmp_path, fake_git
    ):
        image = Mock()
        outpost.clone_private_repo(
            image,
            "https://github.com/acme/widgets",
            "/root/workspace/widgets",
            token_secret=_secret_ref(None),
        )
        [command] = image.run_commands.call_args.args

        result = _run_shell(command, tmp_path, fake_git, {})

        assert "<secret>" in result.stderr


# ------------------------------------------------------------------------------------------------
# worker_image -- construction only (Modal Image builder calls are lazy, so this is network-free)
# ------------------------------------------------------------------------------------------------


class TestWorkerImage:
    def test_returns_an_image(self):
        assert isinstance(outpost.worker_image(), modal.Image)

    @pytest.mark.parametrize("install_ffmpeg", [True, False])
    @pytest.mark.parametrize("install_chrome", [True, False])
    def test_builds_regardless_of_optional_dependency_flags(self, install_ffmpeg, install_chrome):
        image = outpost.worker_image(install_ffmpeg=install_ffmpeg, install_chrome=install_chrome)
        assert isinstance(image, modal.Image)


class TestOutpostPoolConfig:
    def test_exposes_modal_names_derived_from_the_pool_name(self):
        config = outpost.OutpostPoolConfig(name="demo", pool_id="outpost_env-demo")

        assert config.acceptor_id == "modal-demo"
        assert config.modal_app_name == "outpost-pool-demo"
        assert config.api_url == outpost.DEFAULT_API_URL

    @pytest.mark.parametrize(
        ("field", "kwargs"),
        [
            ("name", {"name": "", "pool_id": "outpost_env-demo"}),
            ("pool_id", {"name": "demo", "pool_id": ""}),
            ("api_url", {"name": "demo", "pool_id": "outpost_env-demo", "api_url": ""}),
        ],
    )
    def test_rejects_empty_required_values(self, field, kwargs):
        with pytest.raises(ValueError, match=f"{field} must not be empty"):
            outpost.OutpostPoolConfig(**kwargs)


# ------------------------------------------------------------------------------------------------
# _session_status
# ------------------------------------------------------------------------------------------------


class TestSessionStatus:
    def test_finds_the_matching_session_by_id(self, monkeypatch):
        def fake_api_request(api_url, token, method, path, body=None):
            assert path == "/opbeta/outposts/devins?phase=claimed&acceptor_id=modal-mypool"
            return {
                "items": [
                    {
                        "metadata": {"session_id": "other-sess"},
                        "status": {"session_status": "running"},
                    },
                    {
                        "metadata": {"session_id": "sess-1"},
                        "status": {"session_status": "suspended"},
                    },
                ]
            }

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)

        status = outpost._session_status("https://api.example.com", "tok", "sess-1", "modal-mypool")

        assert status == "suspended"

    def test_returns_none_when_session_is_no_longer_claimed(self, monkeypatch):
        monkeypatch.setattr(outpost, "_api_request", lambda *a, **k: {"items": []})

        status = outpost._session_status("https://api.example.com", "tok", "sess-1", "modal-mypool")

        assert status is None

    def test_returns_none_when_the_request_itself_fails(self, monkeypatch):
        def fake_api_request(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)

        status = outpost._session_status("https://api.example.com", "tok", "sess-1", "modal-mypool")

        assert status is None


# ------------------------------------------------------------------------------------------------
# _create_sandbox -- Image.from_name is a lazy reference, so "no snapshot published yet" and
# "published snapshot is gone/unusable" both only surface once Sandbox.create resolves it.
# ------------------------------------------------------------------------------------------------


class TestCreateSandbox:
    def test_uses_the_base_image_when_no_snapshot_was_ever_published(self, monkeypatch, capsys):
        base_image = Mock(name="base_image")
        fresh_sandbox = Mock(name="fresh_sandbox")
        create = Mock(side_effect=[modal.exception.NotFoundError("Image not found"), fresh_sandbox])
        monkeypatch.setattr(outpost.modal.Sandbox, "create", create)

        result = outpost._create_sandbox(
            Mock(), base_image, "outpost-x-session-y-snapshot", "/root/workspace", 1800
        )

        assert result is fresh_sandbox
        assert create.call_args.kwargs["image"] is base_image
        assert capsys.readouterr().err == ""  # the common case shouldn't print a warning

    def test_resumes_from_the_snapshot_when_one_exists(self, monkeypatch):
        resumed_sandbox = Mock(name="resumed_sandbox")
        create = Mock(return_value=resumed_sandbox)
        monkeypatch.setattr(outpost.modal.Sandbox, "create", create)
        resumed_image = Mock(name="resumed_image")
        monkeypatch.setattr(outpost.modal.Image, "from_name", Mock(return_value=resumed_image))

        result = outpost._create_sandbox(
            Mock(), Mock(name="base_image"), "outpost-x-session-y-snapshot", "/root/workspace", 1800
        )

        assert result is resumed_sandbox
        assert create.call_count == 1
        assert create.call_args.kwargs["image"] is resumed_image

    def test_propagates_unexpected_snapshot_errors_instead_of_starting_fresh(
        self, monkeypatch, capsys
    ):
        create = Mock(side_effect=RuntimeError("modal API unavailable"))
        monkeypatch.setattr(outpost.modal.Sandbox, "create", create)
        monkeypatch.setattr(
            outpost.modal.Image, "from_name", Mock(return_value=Mock(name="resumed_image"))
        )

        with pytest.raises(RuntimeError, match="modal API unavailable"):
            outpost._create_sandbox(
                Mock(),
                Mock(name="base_image"),
                "outpost-x-session-y-snapshot",
                "/root/workspace",
                1800,
            )

        assert create.call_count == 1
        assert capsys.readouterr().err == ""


class TestRunSession:
    def test_releases_the_claim_when_sandbox_creation_fails(self, monkeypatch):
        release = Mock()
        monkeypatch.setattr(outpost, "_release", release)
        monkeypatch.setattr(
            outpost, "_create_sandbox", Mock(side_effect=RuntimeError("no capacity"))
        )

        with pytest.raises(RuntimeError, match="no capacity"):
            outpost.run_session(
                Mock(),
                Mock(),
                "sess-1",
                pool_name="mypool",
                pool_id="outpost_env-abc",
                api_url="https://api.example.com",
                sidecar_image_id="im-123",
            )

        release.assert_called_once_with(
            "https://api.example.com", "test-token", "sess-1", "modal-mypool"
        )
