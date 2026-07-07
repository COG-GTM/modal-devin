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

Edit the generated `pools/<name>.py` to extend the base image with your own
repos (`git clone` in `.run_commands(...)`) before deploying.
