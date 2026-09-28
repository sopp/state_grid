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

## Run
    pip install -r requirements.txt
    SGCC_PASSWORD='...' python sgcc_sidecar.py --account you@example.com --check   # no login
    SGCC_PASSWORD='...' python sgcc_sidecar.py --account you@example.com           # login + fetch

Use a **strong, unique** password value; never put it in the command line.
`--check` proves launch + warm-up without spending a login attempt.

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
