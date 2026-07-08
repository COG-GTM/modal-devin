"""Tests for the network-free logic in modal_devin.outpost: the HTTP client, the poll/claim/
dispatch loop, private-repo cloning, and image construction.

Sandbox/sidecar execution and real Modal image builds (`worker_image(...).build(...)`,
`build_sidecar_image_id`, `run_session`) talk to the live Modal API and aren't covered here --
they're exercised by hand against a real workspace instead.
"""

from __future__ import annotations

import io
import json
import os
import subprocess
import urllib.error
from types import SimpleNamespace
from unittest.mock import Mock, call

import modal
import pytest

from modal_devin import outpost


# ------------------------------------------------------------------------------------------------
# _api_request
# ------------------------------------------------------------------------------------------------


class _RecordingUrlopen:
    """Stand-in for `urllib.request.urlopen`: records every `Request` it's called with and
    answers with a canned JSON body, so tests can inspect exactly what `_api_request` sent."""

    def __init__(self):
        self.response_body = {}
        self.requests: list[urllib.request.Request] = []

    def __call__(self, req):
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
            "https://api.example.com", "tok", "POST",
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
            pool_name="mypool", pool_id="outpost_env-abc",
            api_url="https://api.example.com", run_session_fn=run_session_fn,
        )

        run_session_fn.spawn.assert_has_calls([call("sess-1"), call("sess-2")])
        assert claimed_paths == [
            "/opbeta/outposts/devins/sess-1/claim",
            "/opbeta/outposts/devins/sess-2/claim",
        ]

    def test_skips_a_session_claimed_by_another_worker(self, monkeypatch):
        def fake_api_request(api_url, token, method, path, body=None):
            if method == "GET":
                return _pending("sess-1", "sess-2")
            if "sess-1" in path:
                raise urllib.error.HTTPError(path, 409, "Conflict", hdrs=None, fp=None)
            return {"status": {}}

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool", pool_id="outpost_env-abc",
            api_url="https://api.example.com", run_session_fn=run_session_fn,
        )

        run_session_fn.spawn.assert_called_once_with("sess-2")

    def test_keeps_going_after_an_unexpected_claim_error(self, monkeypatch):
        def fake_api_request(api_url, token, method, path, body=None):
            if method == "GET":
                return _pending("sess-1", "sess-2")
            if "sess-1" in path:
                raise urllib.error.HTTPError(path, 500, "Server Error", hdrs=None, fp=None)
            return {"status": {}}

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool", pool_id="outpost_env-abc",
            api_url="https://api.example.com", run_session_fn=run_session_fn,
        )

        run_session_fn.spawn.assert_called_once_with("sess-2")

    def test_returns_quietly_when_polling_itself_fails(self, monkeypatch):
        def fake_api_request(*args, **kwargs):
            raise urllib.error.URLError("connection refused")

        monkeypatch.setattr(outpost, "_api_request", fake_api_request)
        run_session_fn = Mock()

        outpost.poll_and_dispatch(
            pool_name="mypool", pool_id="outpost_env-abc",
            api_url="https://api.example.com", run_session_fn=run_session_fn,
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
            pool_name="mypool", pool_id="outpost_env-abc",
            api_url="https://api.example.com", run_session_fn=Mock(),
        )

        assert api_urls_used == ["http://caddy:8686"]


# ------------------------------------------------------------------------------------------------
# clone_private_repo -- validation and wiring
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def secret():
    return SimpleNamespace(name="github-clone-token")


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

    @pytest.mark.parametrize("bad_env_var", ["123TOKEN", "GIT TOKEN", "", "GIT-TOKEN"])
    def test_rejects_a_token_env_var_that_is_not_a_shell_identifier(
        self, fake_image, secret, bad_env_var
    ):
        with pytest.raises(ValueError, match="valid shell identifier"):
            outpost.clone_private_repo(
                fake_image, "https://github.com/org/repo", "/root/workspace/x",
                token_secret=secret, token_env_var=bad_env_var,
            )


class TestClonePrivateRepoWiring:
    def test_passes_the_secret_through_to_run_commands(self, fake_image, secret):
        outpost.clone_private_repo(
            fake_image, "https://github.com/org/repo", "/root/workspace/repo",
            token_secret=secret,
        )

        assert fake_image.run_commands.call_args.kwargs["secrets"] == [secret]

    def test_returns_the_image_that_run_commands_produces(self, fake_image, secret):
        result = outpost.clone_private_repo(
            fake_image, "https://github.com/org/repo", "/root/workspace/repo",
            token_secret=secret,
        )

        assert result is fake_image.run_commands.return_value


# ------------------------------------------------------------------------------------------------
# clone_private_repo -- the shell command it builds, run for real through `sh`
#
# `run_commands`'s argument is a shell script that will execute verbatim during the image build.
# Asserting on the string misses exactly the bug this once had: shlex.quote-ing the whole
# credential URL wrapped it in single quotes, which silently disables `$VAR` expansion in
# sh/bash -- git received the literal 8 characters "$GIT_CLONE_TOKEN", not the actual secret.
# Running the command for real through `sh`, with a fake `git` on PATH, is what actually proves
# the secret gets substituted rather than passed through unexpanded.
# ------------------------------------------------------------------------------------------------


@pytest.fixture
def fake_git(tmp_path):
    """A `git` stub that appends its argv (one arg per line, invocations separated by `---`) to
    a log file instead of touching the network, so cloning is instant and offline."""
    log_path = tmp_path / "git-invocations.log"
    git_path = tmp_path / "git"
    git_path.write_text(
        "#!/bin/sh\n"
        "for a in \"$@\"; do printf '%s\\n' \"$a\" >> \"$GIT_LOG\"; done\n"
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
    def test_substitutes_the_real_secret_value_into_the_clone_url(self, tmp_path, fake_git):
        image = Mock()
        outpost.clone_private_repo(
            image, "https://github.com/acme/widgets", "/root/workspace/widgets",
            token_secret=SimpleNamespace(name="github-clone-token"),
        )
        [command] = image.run_commands.call_args.args

        result = _run_shell(command, tmp_path, fake_git, {"GIT_CLONE_TOKEN": "s3cr3t-value"})

        assert result.returncode == 0, result.stderr
        clone_argv, remote_argv = _git_invocations(fake_git)
        assert clone_argv == [
            "clone",
            "https://x-access-token:s3cr3t-value@github.com/acme/widgets",
            "/root/workspace/widgets",
        ]
        assert remote_argv == [
            "-C", "/root/workspace/widgets", "remote", "set-url", "origin",
            "https://github.com/acme/widgets",
        ]

    def test_never_writes_the_secret_to_the_cloned_remote_url(self, tmp_path, fake_git):
        """Belt-and-suspenders: even though `fake_git` never creates a real .git/config, assert
        the final `remote set-url` call -- the one whose effect actually lands in the built image
        -- only ever sees the plain repo_url, never the credentialed one."""
        image = Mock()
        outpost.clone_private_repo(
            image, "https://github.com/acme/widgets", "/root/workspace/widgets",
            token_secret=SimpleNamespace(name="github-clone-token"),
        )
        [command] = image.run_commands.call_args.args

        _run_shell(command, tmp_path, fake_git, {"GIT_CLONE_TOKEN": "s3cr3t-value"})

        _clone_argv, remote_argv = _git_invocations(fake_git)
        assert not any("s3cr3t-value" in arg for arg in remote_argv)

    def test_fails_fast_with_a_clear_message_when_the_token_is_unset(self, tmp_path, fake_git):
        image = Mock()
        outpost.clone_private_repo(
            image, "https://github.com/acme/widgets", "/root/workspace/widgets",
            token_secret=SimpleNamespace(name="github-clone-token"),
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
            image, "https://github.com/acme/widgets", "/root/workspace/widgets",
            token_secret=SimpleNamespace(name=None),
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
