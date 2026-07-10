"""The public, Worker-centered modal-devin API."""

from __future__ import annotations

import inspect
import math
import os
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Concatenate, ParamSpec, Self, cast

import modal

from modal_devin._config import DEFAULT_API_URL, WorkerConfig, WorkerSettings
from modal_devin._exceptions import ConfigurationError
from modal_devin._runtime import (
    dispatch_pending_sessions as _dispatch_pending_sessions,
)
from modal_devin._runtime import execute_session as _execute_session
from modal_devin.images import _controller_image, _finalize_worker_image, _worker_image

_SANDBOX_RESERVED_OPTIONS = {"readiness_probe", "timeout", "workdir"}
_TERMINATION_MARGIN_SECONDS = 30
_P = ParamSpec("_P")


def _sandbox_create_signature(
    create: Callable[_P, modal.Sandbox],
) -> Callable[
    [Callable[..., None]],
    Callable[Concatenate[object, str, _P], None],
]:
    """Type a Worker method as a positional prefix plus ``Sandbox.create``."""
    create_signature = inspect.signature(create)

    def decorate(
        method: Callable[..., None],
    ) -> Callable[Concatenate[object, str, _P], None]:
        parameters = (
            inspect.Parameter("self", inspect.Parameter.POSITIONAL_OR_KEYWORD),
            inspect.Parameter(
                "session_id",
                inspect.Parameter.POSITIONAL_OR_KEYWORD,
                annotation=str,
            ),
            *create_signature.parameters.values(),
        )
        cast(Any, method).__signature__ = create_signature.replace(
            parameters=parameters,
            return_annotation=None,
        )
        return cast(Callable[Concatenate[object, str, _P], None], method)

    return decorate


def _validated_sandbox_options(options: dict[str, object]) -> dict[str, Any]:
    conflicts = sorted(options.keys() & _SANDBOX_RESERVED_OPTIONS)
    if conflicts:
        joined = ", ".join(conflicts)
        raise ConfigurationError(f"Sandbox options cannot override runtime invariants: {joined}")
    return cast(dict[str, Any], options)


def _token_from_env() -> str:
    try:
        token = os.environ["DEVIN_OUTPOSTS_TOKEN"]
    except KeyError as error:
        raise ConfigurationError(
            "DEVIN_OUTPOSTS_TOKEN is unavailable; attach the Devin token Modal Secret "
            "to this function"
        ) from error
    if not token:
        raise ConfigurationError("DEVIN_OUTPOSTS_TOKEN must not be empty")
    return token


@dataclass(frozen=True, slots=True, init=False)
class Worker:
    """A configured Devin Outposts worker runtime.

    The generated application owns its Modal app, functions, schedule, secrets, and
    deployment policy. ``Worker`` supplies the standard image recipe and the durable
    domain operations used by those function entrypoints.
    """

    _config: WorkerConfig
    settings: WorkerSettings

    def __init__(
        self,
        name: str,
        *,
        pool_id: str,
        api_url: str = DEFAULT_API_URL,
        settings: WorkerSettings | None = None,
    ) -> None:
        if settings is not None and not isinstance(settings, WorkerSettings):  # type: ignore[unreachable]
            raise TypeError(f"settings must be WorkerSettings, got {type(settings).__name__}")
        object.__setattr__(
            self, "_config", WorkerConfig(name=name, pool_id=pool_id, api_url=api_url)
        )
        object.__setattr__(
            self,
            "settings",
            WorkerSettings() if settings is None else settings,
        )

    @classmethod
    def from_env(
        cls,
        name: str,
        *,
        pool_id: str,
        api_url: str = DEFAULT_API_URL,
    ) -> Self:
        """Create a worker with operational settings read from ``MODAL_DEVIN_*``."""
        return cls(
            name,
            pool_id=pool_id,
            api_url=api_url,
            settings=WorkerSettings.from_env(),
        )

    @property
    def name(self) -> str:
        return self._config.name

    @property
    def app_name(self) -> str:
        """The conventional Modal application name for this worker."""
        return self._config.app_name

    @property
    def session_function_timeout_seconds(self) -> int:
        """The minimum safe timeout for the outer Modal session function."""
        status_request_budget = self.settings.status_attempts * self.settings.api_timeout_seconds
        status_retry_delay_budget = self.settings.status_retry_delay_seconds * sum(
            range(1, self.settings.status_attempts)
        )
        claim_and_release_budget = 2 * self.settings.api_timeout_seconds
        return math.ceil(
            self.settings.session_timeout_seconds
            + self.settings.sandbox_ready_timeout_seconds
            + self.settings.sidecar_ready_timeout_seconds
            + status_request_budget
            + status_retry_delay_budget
            + self.settings.snapshot_timeout_seconds
            + claim_and_release_budget
            + _TERMINATION_MARGIN_SECONDS
        )

    def controller_image(self, *, python_version: str = "3.12") -> modal.Image:
        """Return the lightweight image used by scheduled control-plane functions."""
        return _controller_image(python_version=python_version)

    def base_image(
        self,
        *,
        python_version: str = "3.12",
        install_ffmpeg: bool = True,
        install_chrome: bool = True,
    ) -> modal.Image:
        """Return the standard worker image, still open to user build steps."""
        return _worker_image(
            python_version=python_version,
            install_ffmpeg=install_ffmpeg,
            install_chrome=install_chrome,
        )

    def prepare_image(self, image: modal.Image) -> modal.Image:
        """Add the modal-devin runtime after all user image customization.

        This is the terminal image-composition step. Pass the returned image to both
        the Modal session function and :meth:`run_session`.
        """
        if not isinstance(image, modal.Image):  # type: ignore[unreachable]
            raise TypeError(f"image must be a modal.Image, got {type(image).__name__}")
        return _finalize_worker_image(image)

    @_sandbox_create_signature(modal.Sandbox.create)
    def run_session(
        self,
        session_id: str,
        *sandbox_args: str,
        **sandbox_options: object,
    ) -> None:
        """Run one session dispatched by :meth:`dispatch_pending_sessions`.

        Sandbox keyword options are inherited from :meth:`modal.Sandbox.create`, so
        editors track the installed Modal SDK. modal-devin owns the Sandbox command,
        timeout, workdir, and readiness probe; those options cannot be overridden.
        """
        if sandbox_args:
            raise TypeError(
                "run_session does not accept Sandbox command arguments; "
                "modal-devin owns the Sandbox command"
            )
        app = sandbox_options.pop("app", None)
        image = sandbox_options.pop("image", None)
        if not isinstance(app, modal.App):  # type: ignore[unreachable]
            raise TypeError("run_session requires app= with a modal.App")
        if not isinstance(image, modal.Image):  # type: ignore[unreachable]
            raise TypeError("run_session requires image= with a modal.Image")
        options = _validated_sandbox_options(sandbox_options)
        _execute_session(
            app=app,
            image=image,
            config=self._config,
            settings=self.settings,
            session_id=session_id,
            token=_token_from_env(),
            sandbox_options=options,
        )

    def dispatch_pending_sessions(
        self,
        spawn_session: Callable[[str], object],
    ) -> None:
        """Dispatch every pending session; each invocation claims when it starts."""
        if not callable(spawn_session):
            raise TypeError("spawn_session must be callable")
        _dispatch_pending_sessions(
            config=self._config,
            settings=self.settings,
            spawn_session=spawn_session,
            token=_token_from_env(),
        )
