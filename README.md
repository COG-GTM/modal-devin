# modal-devin

Run Devin Outposts workers on Modal instead of dedicated VMs.

```
modal-devin outpost create my-pool --pool-id outpost_env-...
modal deploy pools/my_pool.py
```

This deploys two Modal functions per pool:

- `poll_and_dispatch` — cron-scheduled (every 30s), polls the Outposts fleet
  API for pending sessions in the pool, atomically claims each one, and
  spawns `run_session` for it. No persistent process to babysit.
- `run_session` — runs one claimed session to completion. Starts a local
  Caddy sidecar that holds the real `DEVIN_OUTPOSTS_TOKEN` and injects it
  into requests to the Devin API; `devin worker start` itself only ever sees
  a dummy token pointed at the sidecar, so the token never lands in an env
  that Devin's own docs say the agent's shell inherits.

The generated `pools/<name>.py` is a thin entrypoint, not a copy-pasted copy
of the logic: `run_session` and `poll_and_dispatch` are just `@app.function`
stubs (Modal requires the decorated functions themselves to live at module
scope in the deployed file) whose bodies call straight into
`modal_devin.outpost`, which is mounted into the pool's image via
`.add_local_python_source("modal_devin")` — no `pip install`, no version
pinning, no separate publish step. That means a fix to the polling/claim or
sidecar/token-isolation logic in `modal_devin/outpost.py` applies to every
pool the next time it's deployed, instead of needing a hand-patch per file.

Edit the generated `pools/<name>.py` to extend the base image with your own
repos (`git clone` in `.run_commands(...)`) before deploying.
