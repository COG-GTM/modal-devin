"""Outpost pool: modal-local."""

import modal

from modal_devin import outpost

POOL_NAME = "modal-local"
POOL_ID = "outpost_env-c2b537724d234336837b2c817d985904"
API_URL = "https://api.beta.devinenterprise.com"

# install_ffmpeg/install_chrome default to on (screen recording / browser tools "just work");
# pass False for either to skip it and get a leaner, faster-to-build image.
image = outpost.worker_image()
# Devin requires every session repo checked out as a direct subdirectory of the workdir
# `devin worker start` runs from (outpost.run_session runs it from /root/workspace) -- clone
# each repo straight into /root/workspace/<repo>, not into a nested path:
# image = (
#     image.run_commands("git clone https://github.com/your-org/app /root/workspace/app")
#     .run_commands("git clone https://github.com/your-org/infra /root/workspace/infra")
# )

app = modal.App(f"outpost-pool-{POOL_NAME}")
secret = modal.Secret.from_name("devin-outposts-token")
sidecar_image_id = outpost.build_sidecar_image_id(POOL_NAME)


@app.function(image=image, secrets=[secret], timeout=outpost.SESSION_TIMEOUT_SECS + 60)
def run_session(session_id: str):
    outpost.run_session(
        app, image, session_id,
        pool_name=POOL_NAME, pool_id=POOL_ID, api_url=API_URL,
        sidecar_image_id=sidecar_image_id,
    )


@app.function(image=image, secrets=[secret], schedule=modal.Period(seconds=outpost.POLL_INTERVAL_SECS))
def poll_and_dispatch():
    outpost.poll_and_dispatch(
        pool_name=POOL_NAME, pool_id=POOL_ID, api_url=API_URL,
        run_session_fn=run_session,
    )
