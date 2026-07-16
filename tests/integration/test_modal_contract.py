from __future__ import annotations

import os

import modal
import pytest

from modal_devin import Worker

worker = Worker(
    "modal-devin-integration",
    outpost_id="unused-in-integration-test",
)
app = modal.App("modal-devin-integration")
secret = modal.Secret.from_dict({"DEVIN_OUTPOSTS_TOKEN": "integration-placeholder"})
base_image = modal.Image.debian_slim().run_commands("mkdir -p /root/workspace")
image = worker.prepare_image(base_image)
controller_image = worker.controller_image()


@app.function(
    name="session",
    image=image,
    secrets=[secret],
    timeout=worker.session_function_timeout_seconds,
)
def session(session_id: str) -> None:
    worker.run_session(session_id, app=app, image=image)


@app.function(
    name="scheduler",
    image=controller_image,
    secrets=[secret],
    schedule=modal.Period(seconds=worker.settings.scheduler_interval_seconds),
)
def scheduler() -> None:
    worker.dispatch_pending_sessions(session.spawn)


@pytest.mark.integration
@pytest.mark.skipif(
    os.environ.get("MODAL_DEVIN_RUN_MODAL_INTEGRATION") != "1",
    reason="set MODAL_DEVIN_RUN_MODAL_INTEGRATION=1 to create an ephemeral Modal app",
)
def test_generated_application_contract_hydrates_against_modal():
    """Exercise normal Modal registration and image hydration without contacting Devin."""
    with app.run():
        assert app.registered_functions["scheduler"].object_id
        assert app.registered_functions["session"].object_id
