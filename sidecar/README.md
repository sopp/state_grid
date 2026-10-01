# 95598 browser sidecar

Prototype. Replaces the login half of `custom_components/state_grid`, which is dead since the
2026-09 State Grid upgrade.

## Why a browser is now mandatory
The signed HTTP API itself is still there (same paths, same SM4/SM2 envelope), but 95598 moved
the session key into the page: the browser generates an SM2 keypair, keeps the private half in
a Vuex getter, and the server encrypts every response to the client's public key. So no
offline client can decrypt a response, and captured headers/cookies cannot be replayed.
Measured 2026-09-26; see the notes in `sgcc_sidecar.py`.

## What this does
1. Launches Chrome with a **persistent profile** (the profile *is* the identity).
2. Warms Tencent's captcha identity cookie `TDC_itoken`. Without it, `f06` is rejected
   outright and the page shows `RK001` with no challenge ever rendered.
3. Submits the normal password login.
4. If Tencent serves a point-click challenge, solves it with `captcha_solver/`.
5. Reads data by letting the SPA decrypt its own responses, captured through a `JSON.parse`
   hook installed before page scripts run.

## Pushing into Home Assistant
The integration registers a webhook on first setup and logs its path once:
`/api/webhook/<128-bit random id>` (LAN-only). Point the sidecar at it through the environment:

    HA_WEBHOOK_URL='http://<nas-ip>:8123' HA_WEBHOOK_TOKEN='<the id printed in the HA log>' \
    SGCC_PASSWORD='...' python sgcc_sidecar.py --account you@example.com --captcha llm \
        --by-meter /electricityCharge --push

`--push` only sends the per-meter bundle (`<json-out>.meters.json`), because that is the only
data whose meter attribution is self-verified. On the HA side `data_client.__fetch` checks that
cache before signing a request, serves each payload once, and otherwise behaves exactly as
before — so a page the sidecar could not reach still falls back to the normal HTTP path.

Daily unattended run: the profile keeps 95598's session cookies, so a scheduled
`--harvest-only --by-meter ... --push` costs no login while the cookies live, and only falls
back to a full login run when they expire.

## Run
    pip install -r requirements.txt
    SGCC_PASSWORD='...' python sgcc_sidecar.py --account you@example.com --check   # no login
    SGCC_PASSWORD='...' python sgcc_sidecar.py --account you@example.com           # login + fetch

Use a **strong, unique** password value; never put it in the command line.
`--check` proves launch + warm-up without spending a login attempt.

## Running it as a container
`docker-compose.yml` + `Dockerfile` put the whole loop on the NAS next to HA. Build from the repo
root (the image copies the integration's own captcha solver so there is only one copy of it):

    cp sidecar/docker-compose.env.example sidecar/.env   # then fill it in, chmod 600 .env
    docker compose -f sidecar/docker-compose.yml up -d --build
    docker compose -f sidecar/docker-compose.yml exec sgcc-sidecar /app/run_once.sh   # 手动跑一轮

`run_once.sh` does one round: reuse the profile's live session if it still works (costs no login
attempt), otherwise log in with `--captcha llm`, and if that identifier is refused it tries
`SGCC_EMAIL_ACCOUNT` once — it never retries the same identifier back to back, because the block
follows attempt density rather than the clock. `run_daily.sh` (the container default CMD) runs it
at `SGCC_RUN_AT`, retries once after `SGCC_RETRY_MIN` minutes on failure, then waits for the next
day.

`/data` holds the Chrome profile, the last harvest and the per-run logs. That profile *is* the
browser identity (瑞数/TDID/`TDC_itoken` live in it), so deleting it means building a new one with
real login attempts — keep the volume.

Known gaps this loop does not fill: the ladder endpoint (`c04/f03`) is not reachable from any page
we have found, and per-month daily backfill is not harvested either. Those requests fall through to
the integration's normal path, and in push-fed mode the integration no longer logs in by itself, so
they simply stay empty instead of burning quota.

## Where the captcha LLM config comes from
Three places, deliberately separate:

- **HA integration** (it solves its own logins): UI 配置项 `llm_api_key` / `llm_base_url` / `llm_model`,
  stored in `.storage/state_grid.config`.
- **sidecar on a workstation**: `SGCC_LLM_KEY/BASE/MODEL` (or one `SGCC_LLM` JSON blob).
- **sidecar in the container**: same three vars, from `.env`.

Set `SGCC_HA_STORE=/path/to/.storage/state_grid.config` (plus a `:ro` mount of HA's config dir)
and the sidecar re-reads those three keys from HA's store at the start of every round and prefers
them - so changing the model in the HA UI is enough. It logs one line when the store config
differs from its own, and falls back to the env vars if the store file is missing or has no key.

Before turning that on, measure it: on the same 7 labeled captcha samples the store's endpoint
scored 4/7 while the `.env` endpoint scored 5/7, so "point at the store" is not automatically an
upgrade. Align the two first, then switch.

## Vendored code and licence
`captcha_solver/` is copied unmodified from https://github.com/renxiaoyaoo/ha-95598
(Apache-2.0, see `LICENSE.ha-95598`). It is used under that licence and unmodified except for
none — keep it that way so upstream fixes stay mergeable.

## Windows gotcha
`--user-data-dir` must be an **absolute** path or chromedriver crashes at startup with
"Chrome failed to start: crashed / DevToolsActivePort file doesn't exist". Already handled.

## Status
Validated: Chrome drives the live login page, identity warm-up creates `TDC_itoken`, the
solver runs under OpenCV 5. Not yet validated end-to-end: needs one real login to confirm the
captcha challenge and the data harvest.
