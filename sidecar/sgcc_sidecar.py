"""95598 browser sidecar — logs in and reads electricity data for state_grid.

Why this exists: after the 2026-09 95598 upgrade the signed HTTP API is still live, but the
session key is now a client-side SM2 keypair whose private half never leaves the page
($tools.doDecrypt reads it from a Vuex getter), so no offline/aiohttp client can decrypt a
response. Verified 2026-09-26. Therefore the fetch must happen inside a real browser.

Design: drive Chrome, let the SPA do its own encrypt/decrypt, and harvest the decrypted
JSON by wrapping JSON.parse before app code runs. Login uses the same password form a human
uses; the Tencent point-click captcha is solved by captcha_solver/ (vendored verbatim from
https://github.com/renxiaoyaoo/ha-95598, Apache-2.0 — see LICENSE.ha-95598).

Two findings this code depends on, both measured rather than assumed:
  * a brand-new profile gets `f06` -> "RK001" outright because it lacks Tencent's captcha
    identity cookie TDC_itoken; instantiating the site's own TencentCaptcha widget once
    creates it, after which f06 behaves normally (offers a real challenge).
  * the browser profile IS the identity, so it must persist between runs.

Usage:
  SGCC_PASSWORD='...' python sgcc_sidecar.py --account tiejiang@qq.com --check   # no login
  SGCC_PASSWORD='...' python sgcc_sidecar.py --account tiejiang@qq.com           # login + fetch
"""
from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import random
import re
import sys
import time
import urllib.request
from pathlib import Path

import io
from PIL import Image
from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait

from captcha_solver.tencent import TencentCaptchaHandler

# A real browser session (HAR, 2026-09-28) uses the bare host; www. and bare are different
# origins, so localStorage/sessionStorage, the 瑞数 cookie, the chosen region and the APM
# device identity are all separate. Overridable to A/B that without editing constants.
BASE = os.environ.get("SGCC_BASE", "https://www.95598.cn")
LOGIN_URL = BASE + "/osgweb/login"
HOME_URL = BASE + "/osgweb/my95598"
def detect_chrome_ua() -> str:
    """Build a UA that matches the installed Chrome's real major version.

    Overriding the UA with a *different* major than the browser's own sec-ch-ua client hints
    is a self-inflicted tamper signal: Chrome 153 advertising "Chrome/131" in the UA while the
    hints say 153 got us an instant f06 rejection, whereas a matching UA reached a real captcha
    challenge. Headless also reports "HeadlessChrome", so the override is still required.
    """
    ver = None
    for base in (Path(r"C:\Program Files\Google\Chrome\Application"),
                 Path(r"C:\Program Files (x86)\Google\Chrome\Application")):
        if base.is_dir():
            for child in sorted(base.iterdir(), reverse=True):
                if child.is_dir() and child.name[0].isdigit():
                    ver = child.name
                    break
        if ver:
            break
    if not ver:
        ver = os.environ.get("SGCC_CHROME_VERSION", "153.0.0.0")
    return (f"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            f"(KHTML, like Gecko) Chrome/{ver} Safari/537.36")


UA = detect_chrome_ua()
CAPTCHA_APPID = "190586614"          # aid seen on the live login page
PASSWORD_TAB = '//*[@id="login_box"]/div[1]/div[1]/div[2]/span'
AGREEMENT = '//*[@id="login_box"]/div[2]/div[1]/form/div[1]/div[3]/div/span[2]'
LOGGED_IN_MARKERS = ("退出登录", "我的95598", "户号")
BLOCK_MARKERS = ("RK001", "网络连接超时", "请求异常", "页面停留时间过长", "重新登录")

log = logging.getLogger("sgcc-sidecar")

# Installed before any page script runs, and re-injected on every navigation, so we can read
# the app's own decrypted API payloads without ever holding a key.
HARVEST_JS = r"""
(() => {
  if (window.__harvestInstalled) return;
  window.__harvestInstalled = true;
  window.__apiResponses = [];
  window.__apiRequests = [];
  const origParse = JSON.parse;
  const sink = (rec) => {
    // window.* is lost on every navigation, which hid the guard call behind a bounce to
    // /login. sessionStorage survives same-tab navigations, so the log covers the whole run.
    try {
      const a = JSON.parse(sessionStorage.getItem('__hlog') || '[]');
      if (a.length < 400) { a.push(rec); sessionStorage.setItem('__hlog', JSON.stringify(a)); }
    } catch (e) {}
  };
  JSON.parse = function (text, ...rest) {
    const out = origParse.call(JSON, text, ...rest);
    try {
      if (out && typeof out === 'object' && window.__apiResponses.length < 300) {
        const keys = Object.keys(out);
        // Not just {data,code} envelopes: some endpoints hand back their business object
        // directly (the ladder payload is {billRead,pointList,readList}), and filtering those
        // out is why c04/f03 never appeared in any harvest despite the request firing.
        const known = ['data','code','billRead','pointList','readList','sevenEleList',
                       'mothEleList','powerUserList','payList','consList','bizrt'];
        if (keys.some(k => known.includes(k))) {
          const rec = { at: Date.now(), keys: keys, value: out,
                        url: window.__lastApiUrl || null, href: location.pathname };
          window.__apiResponses.push(rec);
          sink({ at: rec.at, url: rec.url, href: rec.href, keys: keys,
                 code: out.code, msg: out.msg || out.message,
                 errcode: out.errcode, dkeys: out.data && typeof out.data === 'object'
                                                ? Object.keys(out.data).slice(0, 8) : null });
        }
      }
    } catch (e) {}
    return out;
  };
  const origStringify = JSON.stringify;
  JSON.stringify = function (value, ...rest) {
    const s = origStringify.call(JSON, value, ...rest);
    try {
      if (value && typeof value === 'object' && s && s.length < 6000
          && window.__apiRequests.length < 300) {
        window.__apiRequests.push({ at: Date.now(), keys: Object.keys(value), s });
      }
      // Shapes (never values) of the objects that carry auth material, so we can see whether
      // the app refreshed its bearer token and through which endpoint.
      if (value && typeof value === 'object'
          && (value.grant_type || value.refresh_token || value.access_token
              || value.ticket || value.randstr || value._access_token)) {
        window.__plain = window.__plain || [];
        if (window.__plain.length < 200) {
          window.__plain.push({ at: Date.now(), href: location.pathname,
                                url: window.__lastApiUrl || null,
                                shape: Object.keys(value).map(k => k + ':' + String(value[k]).length) });
        }
      }
      // The app splits its bearer across two places: the Authorization header carries the
      // first half, the pre-encryption body carries _access_token (the second half). Capture
      // both so the browser login can hand a usable token to the pure-HTTP client.
      if (value && typeof value === 'object' && value._access_token) {
        window.__auth = window.__auth || { hdr: [], body: [] };
        if (window.__auth.body.length < 40) {
          window.__auth.body.push({ at: Date.now(), url: window.__lastApiUrl || null,
                                    access_tail: String(value._access_token),
                                    t_tail: value._t === undefined ? null : String(value._t) });
        }
      }
    } catch (e) {}
    return s;
  };
  // f06 blocker: gated at runtime by window.__blockF06. Returning without calling the real
  // send() means the login POST never reaches the network at all, so probing our own request
  // costs no login attempt. (Selenium 4.48 has no start_cdp_session, so this is done in JS.)
  window.__blockedReqs = [];
  const oo = XMLHttpRequest.prototype.open;
  const os = XMLHttpRequest.prototype.send;
  const sr = XMLHttpRequest.prototype.setRequestHeader;
  XMLHttpRequest.prototype.open = function (m, u) {
    this.__u = String(u); this.__m = m; this.__h = {};
    if (this.__u.indexOf("/api/") >= 0) { window.__lastApiUrl = this.__u; }
    return oo.apply(this, arguments);
  };
  XMLHttpRequest.prototype.setRequestHeader = function (k, v) {
    try {
      this.__h[k] = v;
      if (k === 'Authorization' || k === 't') {
        window.__auth = window.__auth || { hdr: [], body: [] };
        if (window.__auth.hdr.length < 80) {
          window.__auth.hdr.push({ url: (this.__u || '').split('?')[0], k: k, v: String(v) });
        }
      }
    } catch (e) {}
    return sr.apply(this, arguments);
  };
  XMLHttpRequest.prototype.send = function (b) {
    try {
      const u = this.__u || "";
      if (u.indexOf("/api/") >= 0) {
        sink({ req: u.split("?")[0], m: this.__m, href: location.pathname, at: Date.now(),
               h: Object.keys(this.__h || {}) });
      }
      if (window.__blockF06 && u.indexOf("c44/f06") >= 0) {
        window.__blockedReqs.push({ url: this.__u, method: this.__m,
                                     headers: this.__h, body: typeof b === "string" ? b : null });
        return;
      }
    } catch (e) {}
    return os.apply(this, arguments);
  };
  // Some calls go through fetch, which is why several captured payloads had url=null.
  const of = window.fetch;
  if (of) window.fetch = function (input, init) {
    try {
      const u = typeof input === "string" ? input : (input && input.url) || "";
      if (u.indexOf("/api/") >= 0) {
        sink({ req: u.split("?")[0], m: (init && init.method) || "GET", href: location.pathname,
               at: Date.now() });
        // Member APIs go through fetch, so the XHR setRequestHeader hook never sees their
        // bearer. Capture the two auth headers here too.
        try {
          const h = init && init.headers;
          const grab = (k) => {
            if (!h) return null;
            if (typeof h.get === "function") return h.get(k);
            const hit = Object.keys(h).find(x => x.toLowerCase() === k.toLowerCase());
            return hit ? h[hit] : null;
          };
          window.__auth = window.__auth || { hdr: [], body: [] };
          for (const k of ["Authorization", "t"]) {
            const v = grab(k);
            if (v && window.__auth.hdr.length < 80) {
              window.__auth.hdr.push({ url: u.split("?")[0], k: k, v: String(v) });
            }
          }
        } catch (e) {}
      }
    } catch (e) {}
    return of.apply(this, arguments);
  };
  // Who writes TDC_itoken, and from which script? Patching the cookie setter in the top
  // document is the only way to tell site-side writes from SDK-side ones (a write from inside
  // the captcha iframe will not show up here at all, which is itself the answer).
  try {
    const d = Object.getOwnPropertyDescriptor(Document.prototype, "cookie");
    window.__ckWrites = [];
    Object.defineProperty(document, "cookie", {
      configurable: true,
      get() { return d.get.call(document); },
      set(v) {
        try {
          const name = String(v).split("=")[0].trim();
          if (window.__ckWrites.length < 60) {
            const st = ((new Error()).stack || "").split("\n").slice(1, 5)
              .map(s => s.trim().replace(/^at\s+/, "").slice(0, 90));
            sink({ ck: name, at: Date.now(), href: location.pathname, st });
            window.__ckWrites.push({ ck: name, st });
          }
        } catch (e) {}
        return d.set.call(document, v);
      },
    });
  } catch (e) {}
})();
"""


def enable_f06_block(driver) -> None:
    driver.execute_script("window.__blockF06 = true; window.__blockedReqs = [];")


def read_blocked_f06(driver) -> list[dict]:
    """Normalise what the JS blocker captured into the shape diff_against_har expects."""
    raw = driver.execute_script("return window.__blockedReqs || [];") or []
    out = []
    for r in raw:
        hdrs = r.get("headers") or {}
        body = r.get("body") or ""
        try:
            keys = sorted(json.loads(body).keys()) if body else None
        except Exception:
            keys = "unparseable"
        out.append({
            "url": r.get("url"), "method": r.get("method"),
            "headerNames": sorted(hdrs.keys(), key=str.lower),
            "keyCodeLen": len(hdrs.get("keyCode", "")),
            "deviceTokenTXLen": len(hdrs.get("deviceTokenTX", "")),
            "hasAuthorization": "Authorization" in hdrs,
            "hasSessionId": "sessionId" in hdrs,
            "hasRetryCount": "retryCount" in hdrs,
            "hasT": "t" in hdrs,
            "hasTimestamp": "timestamp" in hdrs,
            "cookieNames": sorted({c.split("=")[0].strip() for c in
                                   (hdrs.get("cookie") or "").split(";") if c.strip()}),
            "bodyLen": len(body), "bodyKeys": keys,
        })
    return out


def cached_chromedriver() -> str:
    """Selenium Manager phones home for known-good-versions on every launch and only after that
    fails falls back to the cache -- a multi-minute stall per run. Use the cached binary directly."""
    roots = [Path.home() / ".cache" / "selenium" / "chromedriver"]
    if os.environ.get("LOCALAPPDATA"):
        roots.insert(0, Path(os.environ["LOCALAPPDATA"]) / "selenium" / "chromedriver")
    for root in roots:
        if not root.exists():
            continue
        found = sorted(root.glob("**/chromedriver.exe")) or sorted(root.glob("**/chromedriver"))
        if found:
            return str(found[-1])
    return ""


# ChromeDriver injects these on window/document before any page script runs; 瑞数 and every
# other anti-bot read them first, which is how our browser got classified as a bot while the
# user's own Chrome on the same account and IP was served a real captcha. Registered as a
# document-start script it runs after chromedriver's own injection, so deleting works.
STEALTH_JS = r"""
(() => {
  const RE = /(^|_)\$*_?cdc_/i;
  const kill = (obj) => {
    if (!obj) return;
    for (const k of Object.getOwnPropertyNames(obj)) {
      if (RE.test(k)) { try { delete obj[k]; } catch (e) {} }
    }
  };
  try { kill(window); kill(document); } catch (e) {}
})();
"""


def make_driver(profile: Path, headless: bool, proxy: str = "") -> webdriver.Chrome:
    # Windows + chromedriver crashes at startup if --user-data-dir is relative
    # ("Chrome failed to start: crashed / DevToolsActivePort file doesn't exist").
    profile = Path(profile).expanduser().resolve()
    profile.mkdir(parents=True, exist_ok=True)
    opts = Options()
    if headless:
        opts.add_argument("--headless=new")
    opts.add_argument(f"--user-data-dir={profile}")
    opts.add_argument(f"--window-size=1366,900")
    opts.add_argument("--lang=zh-CN")
    opts.add_argument("--disable-blink-features=AutomationControlled")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    if proxy:
        opts.add_argument(f"--proxy-server={proxy}")
        host = proxy.rsplit("://", 1)[-1].split(":")[0]
        # Without this Chrome still resolves DNS locally (and UDP can leak the real address),
        # so the site would see the original egress and the experiment would prove nothing.
        opts.add_argument(f"--host-resolver-rules=MAP * ~NOTFOUND, EXCLUDE {host}")
        opts.add_argument("--force-webrtc-ip-handling-policy=disable_non_proxied_udp")
    # A visible-only-once profile keeps 瑞数/TDID reputation warm across runs; do not add
    # --disable-gpu or custom prefs here, they make the fingerprint stand out.
    opts.set_capability("pageLoadStrategy", "eager")
    exe = cached_chromedriver()
    driver = webdriver.Chrome(service=Service(exe), options=opts) if exe \
        else webdriver.Chrome(options=opts)
    try:
        driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": STEALTH_JS})
    except Exception as exc:
        log.warning("stealth script not installed: %s", exc)
    try:
        # The capture hook is deliberately NOT installed here: with it present, tdc.js never
        # writes TDC_itoken. harvest_routes() attaches it after login instead.
        if os.environ.get("SGCC_EAGER_HOOK"):
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HARVEST_JS})
        if headless:
            # headless=new still reports "HeadlessChrome" in the UA, which the site never sees
            # from a normal browser.
            driver.execute_cdp_cmd("Network.setUserAgentOverride",
                                   {"userAgent": detect_chrome_ua()})
    except Exception as exc:  # non-fatal: we can still scrape the DOM
        log.warning("eager setup failed: %s", exc)
    driver.set_page_load_timeout(60)
    driver.implicitly_wait(0)
    return driver


def open_login(driver) -> None:
    try:
        driver.get(LOGIN_URL)
    except Exception as exc:
        log.info("goto login raised %s (continuing; SPA usually still boots)", type(exc).__name__)
    deadline = time.time() + 30
    while time.time() < deadline:
        if driver.execute_script("return !!document.querySelector('#login_box, .user, .wap-login');"):
            return
        time.sleep(1.0)
    log.warning("login form never appeared")


def cookie_names(driver) -> set[str]:
    try:
        return {c["name"] for c in driver.get_cookies()}
    except Exception:
        return set()


def warm_captcha_identity(driver) -> dict:
    """Create TDC_itoken (Tencent's captcha device identity). Without it 95598 rejects the
    login outright with RK001 and never shows a challenge — measured, not theorised.

    We must actually open the widget on every run, not just check for the cookie: the cookie
    persisting from an earlier session did NOT prevent RK001, whereas a run that performed a
    fresh cap_union_prehandle seconds before submitting did reach a real captcha challenge.
    """
    already = "TDC_itoken" in cookie_names(driver)
    # Must be async: the widget resolves over the network. execute_script does NOT await a
    # promise, so use execute_async_script and report through its callback (last argument).
    # Do NOT hide()/destroy() the instance or delete the widget DOM: 95598 registers the captcha
    # callback on that live SDK, and ARC-MX/sgcc_electricity_new documents that submitting while
    # the SDK is not ready makes the server answer RK001 without ever issuing a challenge -- which
    # is exactly what all of our attempts did. Only the click-swallowing mask layer goes away.
    cleanup = r"""
    ['#tCaptchaMaskLayer', '.tencent-captcha__mask-layer', '[class*="tencent-captcha-dy__mask"]']
      .forEach(sel => document.querySelectorAll(sel).forEach(el => el.remove()));
    // Hide (never delete, never destroy()) the widget our warm-up opened: it keeps the SDK's
    // registered callback alive, stops its mask from swallowing the form clicks, and stops
    // has_captcha() from mistaking our own leftover widget for a real challenge. The site
    // instantiates a fresh one on submit, so hiding the current nodes cannot affect it.
    const ours = [...document.querySelectorAll('.tencent-captcha-dy__warp, .tencent-captcha-dy__wrapper,'
                  + ' .tencent-captcha__wrapper, .tencent-captcha-dy__body-wrap')];
    ours.forEach(el => el.style.setProperty('display', 'none', 'important'));
    window.__sgccHidden = (window.__sgccHidden || 0) + ours.length;
    const names = performance.getEntriesByType('resource').map(e => e.name);
    return {mask_gone: document.getElementById('tCaptchaMaskLayer') === null,
            hidden: ours.length,
            sdk: typeof window.TencentCaptcha === 'function',
            instance: !!window.__sgccCap,
            prehandle: names.filter(n => n.indexOf('prehandle') >= 0).length,
            tdc_js: names.filter(n => n.indexOf('tdc.js') >= 0).length};
    """
    script = r"""
    const aid = arguments[0], done = arguments[1];
    if (typeof window.TencentCaptcha !== 'function') { done({status:'sdk-missing'}); return; }
    let settled = false;
    const finish = o => { if (!settled) { settled = true; done(o); } };
    try {
      const cap = new window.TencentCaptcha(aid, () => finish({status:'called-back'}), { needConfirm: false });
      window.__sgccCap = cap;
      cap.show();
      setTimeout(() => finish({status:'shown'}), 6000);
    } catch (e) { finish({status:'error', err:String(e && e.message || e).slice(0,120)}); }
    """
    try:
        driver.set_script_timeout(30)
        res = driver.execute_async_script(script, CAPTCHA_APPID)
    except Exception as exc:
        res = {"status": "eval-failed", "err": str(exc)[:140]}
    try:
        res.update(driver.execute_script(cleanup) or {})
    except Exception as exc:
        res["overlay_clear_failed"] = str(exc)[:100]
    time.sleep(2)
    res["tdc_now"] = "TDC_itoken" in cookie_names(driver)
    res["cookie_preexisted"] = already
    return res


def page_state(driver) -> dict:
    txt = driver.execute_script("return document.body ? (document.body.innerText||'') : ''") or ""
    return {
        "url": driver.current_url,
        "on_login": "/login" in driver.current_url,
        "loggedIn": any(m in txt for m in LOGGED_IN_MARKERS),
        "blocked": next((m for m in BLOCK_MARKERS if m in txt), None),
    }


def captcha_sdk_ready(driver) -> dict:
    """Pre-submit evidence that the Tencent captcha SDK is actually live. Other 95598 projects
    document that submitting before it is ready gets a bare RK001 with no challenge issued, so we
    refuse to spend an attempt in that state instead of guessing afterwards."""
    return driver.execute_script(
        """const names = performance.getEntriesByType('resource').map(e => e.name);
        const w = window.__ckWrites || [];
        const t = w.filter(x => (x.ck || '').indexOf('TDC') === 0);
        return {sdk: typeof window.TencentCaptcha === 'function',
                instance: !!window.__sgccCap,
                tdc: document.cookie.indexOf('TDC_itoken') >= 0,
                prehandle: names.filter(n => n.indexOf('prehandle') >= 0).length,
                // tdc.js is what actually produces the collect payload and the itoken; the
                // known-good capture shows prehandle -> tdc.js -> getcapbysig -> verify.
                tdc_js: names.filter(n => n.indexOf('tdc.js') >= 0).length,
                getcapbysig: names.filter(n => n.indexOf('getcapbysig') >= 0).length,
                cookie_writes: w.map(x => x.ck),
                tdc_writer: t.length ? t[0].st : null};""") or {}


def rect_of(driver, el):
    r = driver.execute_script(
        "const r=arguments[0].getBoundingClientRect();"
        "return [r.left, r.top, r.width, r.height];", el) or []
    return r if len(r) == 4 and r[2] > 0 and r[3] > 0 else None


def _path_to(acts, el, r, jitter=True):
    """Approach the element's centre in a few steps, each anchored to the element itself.

    Cumulative move_by_offset was the bug: the remembered pointer position drifts between
    ActionChains sequences, so the login button -- the lowest target on the card -- got clicked
    somewhere else. The user watched it: the agreement was ticked and the form filled, but the
    登录 button was never pressed and no f06 ever left the browser.
    """
    w, h = r[2], r[3]
    for fx, fy in ((0.08, 0.95), (0.3, 0.72), (0.44, 0.56), (0.5, 0.5)):
        ox = w * fx + (random.uniform(-1.5, 1.5) if jitter else 0)
        oy = h * fy + (random.uniform(-1.5, 1.5) if jitter else 0)
        acts = acts.move_to_element_with_offset(el, ox, oy).pause(random.uniform(0.03, 0.09))
    return acts


def real_click(driver, el, label="") -> bool:
    r = rect_of(driver, el)
    if not r:
        log.info("  %s has no box to click", label or "element")
        return False
    try:
        _path_to(ActionChains(driver), el, r).pause(random.uniform(0.08, 0.2)).click().perform()
        return True
    except Exception as exc:
        log.info("  real click failed on %s: %s", label or "?", type(exc).__name__)
        return False


def focus_el(driver, el) -> bool:
    r = rect_of(driver, el)
    if not r:
        return False
    try:
        _path_to(ActionChains(driver), el, r).pause(random.uniform(0.15, 0.4)).click().perform()
        return True
    except Exception:
        return False


def api_hits(driver, needle: str) -> int:
    """Did a request for this endpoint actually leave the browser? The only honest proof that a
    'submit' submitted."""
    return int(driver.execute_script(
        "return performance.getEntriesByType('resource').map(e => e.name)"
        ".filter(n => n.indexOf(arguments[0]) >= 0).length;", needle) or 0)


def field_at(driver, index: int):
    ins = visible_inputs(driver)
    return ins[index] if len(ins) > index else None


def password_field(driver):
    """The password input, by type if the panel settled, else by form position.

    Element UI re-renders this field during the tab transition, so a type=="password" lookup
    intermittently returns nothing and we would then submit an empty password.
    """
    ins = visible_inputs(driver)
    return next((i for i in ins if i.get_attribute("type") == "password"),
                ins[1] if len(ins) > 1 else None)


def human_type(driver, pick, text: str) -> bool:
    """Type with real keystrokes. `pick` re-resolves the element on each attempt: the SPA
    re-renders the login panel underneath us and a stale handle raises ElementNotInteractable."""
    for attempt in range(3):
        el = pick()
        if el is None:
            time.sleep(1.0)
            continue
        try:
            focus_el(driver, el)
            for ch in str(text):
                el.send_keys(ch)
                time.sleep(random.uniform(0.05, 0.19))
            return True
        except Exception as exc:
            log.info("  typing attempt %d failed (%s); re-resolving the field",
                     attempt + 1, type(exc).__name__)
            time.sleep(1.0)
    return False


def visible_inputs(driver):
    return [i for i in driver.find_elements(By.CSS_SELECTOR, ".el-input__inner") if i.is_displayed()]


def wait_elems(driver, by, value, timeout: float = 10.0):
    """Poll for visible elements; the SPA swaps the login panel in after a click, and
    implicitly_wait(0) means a bare find_elements() would just come back empty."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        els = [e for e in driver.find_elements(by, value) if e.is_displayed()]
        if els:
            return els
        time.sleep(0.4)
    return []


def fill_and_submit(driver, account: str, password: str) -> dict:
    """Log in the way a person does: real pointer path, real keystrokes, real click.

    Measured 2026-09-27: with JS value-setting and btn.click() the site answers RK001 in 0.5s
    and never issues a captcha, while the user clicking the very same window by hand gets a
    normal challenge and logs in. Interaction telemetry, not credentials or identity.
    """
    out = {"steps": []}
    trigger = wait_elems(driver, By.CSS_SELECTOR, ".user", 12)
    if trigger and real_click(driver, trigger[0], ".user"):
        out["steps"].append("user-tab")
    if not wait_elems(driver, By.CSS_SELECTOR, ".el-input__inner", 3) and trigger:
        # The real click may have landed on a different .user node (the page has several), so
        # fall back to the first-in-DOM one the site's own handler uses, then keep going.
        driver.execute_script("document.querySelector('.user')?.click();")
        out["steps"].append("user-tab-js-retry")
    for label, xp in (("password-tab", PASSWORD_TAB),):
        el = wait_elems(driver, By.XPATH, xp, 8)
        if not el:
            out["steps"].append(f"{label}=missing")
            continue
        hit = real_click(driver, el[0], label)
        if not hit:
            driver.execute_script("arguments[0].click();", el[0])
        out["steps"].append(f"{label}={'real' if hit else 'js'}")
        time.sleep(random.uniform(0.4, 0.9))

    ins = wait_elems(driver, By.CSS_SELECTOR, ".el-input__inner", 8)
    out["visible"] = len(ins)
    if len(ins) < 2:
        out["ok"] = False
        return out
    out["typed_acct"] = human_type(driver, lambda: field_at(driver, 0), account)
    time.sleep(random.uniform(0.25, 0.6))
    # The panel re-renders after the first field (and a stale handle raises
    # ElementNotInteractable), so give the password input time to appear before typing.
    deadline = time.time() + 6
    while time.time() < deadline and password_field(driver) is None:
        time.sleep(0.5)
    has_pw = password_field(driver) is not None
    out["typed_pw"] = False
    if has_pw:
        out["typed_pw"] = human_type(driver, lambda: password_field(driver), password)
    a2, p2 = field_at(driver, 0), password_field(driver)
    got_acct = (a2.get_attribute("value") or "") if a2 else ""
    got_pw = (p2.get_attribute("value") or "") if p2 else ""
    out.update(hasPwField=has_pw, acctLen=len(got_acct), pwLen=len(got_pw))
    ins = [e for e in (a2, p2) if e is not None]

    if got_acct != account or (has_pw and len(got_pw) != len(password)):
        # A masked input can swallow synthetic keys; keep the typed events but top the value up
        # through Vue's own path so the model still sees an input event.
        log.info("  keystrokes landed short (%d/%d, %d/%d) -> native setter fallback",
                 len(got_acct), len(account), len(got_pw), len(password))
        driver.execute_script(
            """const set=(el,v)=>{const d=Object.getOwnPropertyDescriptor(
                 Object.getPrototypeOf(el),'value');d.set.call(el,v);
                 el.dispatchEvent(new Event('input',{bubbles:true}));
                 el.dispatchEvent(new Event('change',{bubbles:true}));};
            set(arguments[0], arguments[1]); if(arguments[2]) set(arguments[2], arguments[3]);""",
            a2, account, p2, password)
        out["fallback"] = True

    a3, p3 = field_at(driver, 0), password_field(driver)
    got_acct = (a3.get_attribute("value") or "") if a3 else ""
    got_pw = (p3.get_attribute("value") or "") if p3 else ""
    out.update(acctLen=len(got_acct), pwLen=len(got_pw))
    # Never submit a half-filled form: that is a real failed-login attempt against the account's
    # "5 wrong passwords = 20 min lock" gate, and it is how one attempt got burned on 09:57.
    if got_acct != account or len(got_pw) != len(password):
        out.update(ok=False, submitted=False, reason="fields not verified filled")
        log.error("refusing to submit: phone=%d/%d password=%d/%d -- nothing was sent, so this "
                  "costs no login attempt", len(got_acct), len(account),
                  len(got_pw), len(password))
        return out

    agree = ensure_agreement(driver)
    out["agreement"] = agree
    if os.environ.get("SGCC_SHOT"):
        Path("trace").mkdir(exist_ok=True)
        driver.save_screenshot("trace/agreement.png")
        log.info("agreement probe -> %s (screenshot: trace/agreement.png)", agree)
    if not agree.get("checked"):
        out.update(ok=False, submitted=False, reason="agreement not checked")
        log.error("refusing to submit: agreement still unchecked (%s). The SPA blocks the request "
                  "locally in that state, so nothing is sent and no attempt is spent.", agree)
        return out

    btns = [b for b in driver.find_elements(By.CSS_SELECTOR, "button.el-button--primary")
            if b.is_displayed()]
    out["captcha_sdk"] = captcha_sdk_ready(driver)   # recorded, never a reason to skip
    if not btns:
        out.update(ok=False, submitted=False)
        return out
    if os.environ.get("SGCC_NO_SUBMIT"):
        out.update(ok=True, submitted=False)
        return out
    how = ""
    try:
        btns[0].click()          # real pointer event, and it scrolls the button into view first
        how = "el"
    except Exception as exc:
        how = f"el-failed:{type(exc).__name__}"
    f06 = _wait_f06(driver)
    if not f06:
        # The ActionChains path and a remembered rect both miss this button; dispatch at the
        # coordinate measured right now instead.
        live = driver.execute_script(
            """const b=[...document.querySelectorAll('button.el-button--primary')]
                 .find(x=>x.offsetWidth>0);
               if(!b) return null; const r=b.getBoundingClientRect();
               return [r.left + r.width/2, r.top + r.height/2];""")
        if live:
            driver.execute_script(
                """const e=document.elementFromPoint(arguments[0],arguments[1]);
                if(e){['mousedown','mouseup','click'].forEach(t=>e.dispatchEvent(
                    new MouseEvent(t,{bubbles:true,cancelable:true,
                                      clientX:arguments[0],clientY:arguments[1]})));}""",
                live[0], live[1])
            how += f"+dispatch@{live[0]:.0f},{live[1]:.0f}"
            f06 = _wait_f06(driver)
    out.update(ok=True, clickMethod=how, f06=f06, submitted=bool(f06))
    if not f06:
        log.error("no c44/f06 request left the browser (%s): the submit never happened, so this "
                  "costs no login attempt", how)
    return out


def _wait_f06(driver, tries: int = 6) -> int:
    for _ in range(tries):
        time.sleep(0.8)
        n = api_hits(driver, "c44/f06")
        if n:
            return n
    return 0


def wait_for_challenge(driver, seconds: int = 35) -> str:
    """Returns 'captcha' | 'loggedIn' | 'blocked:<marker>' | 'timeout'."""
    handler = make_handler(driver)
    deadline = time.time() + seconds
    while time.time() < deadline:
        st = page_state(driver)
        if st["loggedIn"] or not st["on_login"]:
            return "loggedIn"
        if st["blocked"]:
            return "blocked:" + st["blocked"]
        if handler.has_captcha(driver):
            return "captcha"
        time.sleep(0.5)
    return "timeout"


def make_handler(driver) -> TencentCaptchaHandler:
    trace = Path(os.environ.get("SGCC_TRACE_DIR", "./trace"))
    trace.mkdir(parents=True, exist_ok=True)
    return TencentCaptchaHandler(
        trace_dir=lambda: trace,
        log_page_state=lambda _d, label: log.info("page state: %s", label),
        step_sleep=lambda _d, label: time.sleep(random.uniform(0.4, 0.9)),
        confirm_login_success=lambda _d: page_state(_d)["loggedIn"] or not page_state(_d)["on_login"],
    )


AGENT_DIR = Path(os.environ.get("SGCC_AGENT_DIR", "trace/agent_task"))

BG_SELECTORS = [
    ".tencent-captcha-dy__point-area",
    ".tencent-captcha-dy__click-type-wrap",
    ".tencent-captcha-dy__verify-bg-img",
    ".tencent-captcha-dy__verify-bg",
    ".tencent-captcha-dy__image-area",
]


def agent_solve_captcha(driver, handler, timeout: int = 300) -> bool:
    """Hand the challenge to an outside vision model through files, then click its answer.

    We write live_strip.png / live_bg.png (plus enlarged copies) and live_request.json, then
    wait for live_reply.json holding {"points": [[x, y], ...]} in native bg pixel space,
    already in reference-strip order.
    """
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    reply = AGENT_DIR / "live_reply.json"
    request = AGENT_DIR / "live_request.json"
    if reply.exists():
        reply.unlink()

    try:
        _, bg_el, strip, bg = capture_challenge(driver, handler)
    except Exception as exc:
        log.error("[agent] captcha elements not found: %s", exc)
        return False

    strip.save(AGENT_DIR / "live_strip.png")
    bg.save(AGENT_DIR / "live_bg.png")
    strip.resize((strip.width * 5, strip.height * 5), Image.LANCZOS).save(AGENT_DIR / "live_strip_5x.png")
    bg.resize((bg.width * 2, bg.height * 2), Image.LANCZOS).save(AGENT_DIR / "live_bg_2x.png")
    request.write_text(json.dumps({
        "at": time.strftime("%H:%M:%S"),
        "strip_size": [strip.width, strip.height],
        "bg_size": [bg.width, bg.height],
        "ask": "read the reference strip left-to-right; return one [x, y] per glyph in that order, "
               "in native bg pixels (top-left of live_bg.png = 0,0)",
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("[agent] challenge written to %s (%dx%d strip, %dx%d bg); waiting up to %ss for reply",
             AGENT_DIR, strip.width, strip.height, bg.width, bg.height, timeout)

    deadline = time.time() + timeout
    points = None
    while time.time() < deadline:
        if reply.exists():
            try:
                data = json.loads(reply.read_text(encoding="utf-8"))
                pts = [[float(p[0]), float(p[1])] for p in data["points"]]
                if pts and all(0 <= x <= bg.width and 0 <= y <= bg.height for x, y in pts):
                    points = pts
                    log.info("[agent] reply: %s points=%s", data.get("note", ""), pts)
                    break
                log.error("[agent] reply points out of bounds or empty: %s", data)
            except Exception as exc:
                log.debug("[agent] reply not readable yet: %s", exc)
        time.sleep(1.0)
    if not points:
        log.error("[agent] no usable reply within %ss", timeout)
        return False
    return click_points_in_order(driver, bg_el, bg, points)


def capture_challenge(driver, handler):
    """Crop the reference strip and the main image out of the live popup."""
    strip_el = WebDriverWait(driver, 8).until(
        EC.presence_of_element_located((By.CSS_SELECTOR, ".tencent-captcha-dy__header-answer img"))
    )
    bg_el = WebDriverWait(driver, 8).until(
        lambda _d: handler.get_visible_descendant(
            _d, BG_SELECTORS, min_width=80, min_height=80) or False
    )
    strip = Image.open(io.BytesIO(handler.capture_element_image(driver, strip_el))).convert("RGB")
    bg = Image.open(io.BytesIO(handler.capture_element_image(driver, bg_el))).convert("RGB")
    return strip_el, bg_el, strip, bg


def llm_solve_captcha(driver, handler) -> bool:
    """Answer the popup with the integration's own LLM solver (same code HA will run)."""
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "custom_components" / "state_grid"))
    try:
        import base64 as b64
        import click_captcha_solver as ccs
    except Exception as exc:
        log.error("[llm] cannot import click_captcha_solver: %s", exc)
        return False

    cfg = json.loads(os.environ.get("SGCC_LLM", "{}"))
    if not (cfg.get("api_key") and cfg.get("model")):
        log.error("[llm] set SGCC_LLM='{\"api_key\":...,\"base_url\":...,\"model\":...}'")
        return False
    ccs.configure_llm(cfg["api_key"],
                      cfg.get("base_url", "https://ark.cn-beijing.volces.com/api/v3"),
                      cfg["model"])
    try:
        _, bg_el, strip, bg = capture_challenge(driver, handler)
    except Exception as exc:
        log.error("[llm] challenge not captured: %s", exc)
        return False

    buf = io.BytesIO()
    strip.save(buf, format="PNG")
    strip_b64 = b64.b64encode(buf.getvalue()).decode("ascii")
    buf = io.BytesIO()
    bg.save(buf, format="PNG")
    bg_b64 = b64.b64encode(buf.getvalue()).decode("ascii")
    points = ccs.solve_click_captcha(strip_b64, bg_b64, bg.width, bg.height)
    if not points:
        log.error("[llm] solver returned no points")
        return False
    log.info("[llm] %s points: %s", len(points), points)
    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    strip.save(AGENT_DIR / "llm_strip.png")
    bg.save(AGENT_DIR / "llm_bg.png")
    (AGENT_DIR / "llm_result.json").write_text(json.dumps(
        {"points": [list(p) for p in points], "bg_size": [bg.width, bg.height]},
        ensure_ascii=False, indent=2), encoding="utf-8")
    return click_points_in_order(driver, bg_el, bg, points)


def click_points_in_order(driver, bg_el, bg, points) -> bool:
    """Click bg-pixel points in strip order, then press the popup's confirm button."""
    rect = bg_el.rect
    x_scale = rect["width"] / bg.width
    y_scale = rect["height"] / bg.height
    for i, (x, y) in enumerate(points):
        # Selenium 4 move_to_element_with_offset is element-center relative (verified locally).
        tx = int(x * x_scale - rect["width"] / 2)
        ty = int(y * y_scale - rect["height"] / 2)
        # A single teleport draws no mousemove samples; Tencent scores pointer behaviour,
        # so approach through one intermediate point before pressing.
        acts = ActionChains(driver)
        acts.move_to_element_with_offset(bg_el, int(tx * 0.55), int(ty * 0.55))
        acts.pause(random.uniform(0.06, 0.14))
        acts.move_to_element_with_offset(bg_el, tx, ty)
        acts.pause(random.uniform(0.08, 0.18))
        acts.click().perform()
        log.info("[captcha] click %s/%s at bg px (%.0f, %.0f)", i + 1, len(points), x, y)
        time.sleep(random.uniform(0.35, 0.7))

    AGENT_DIR.mkdir(parents=True, exist_ok=True)
    (AGENT_DIR / "llm_after_clicks.png").write_bytes(driver.get_screenshot_as_png())
    marks = driver.execute_script(
        """
        const docs = [document]; const seen = new Set(); let n = 0; const hits = [];
        while (docs.length) {
          const doc = docs.pop();
          if (!doc || seen.has(doc)) continue;
          seen.add(doc);
          for (const el of doc.querySelectorAll('[class*="click"],[class*="mark"],[class*="point"]')) {
            const m = (el.className || '').toString().match(/[a-z-]*(click|mark|point)[a-z-]*/i);
            if (m && !hits.includes(m[0])) hits.push(m[0]);
          }
          for (const f of doc.querySelectorAll('iframe,frame')) { try { if (f.contentDocument) docs.push(f.contentDocument); } catch (e) {} }
        }
        return hits.slice(0, 12);
        """)
    log.info("[captcha] mark-ish classes after clicks: %s", marks)

    try:
        confirm = WebDriverWait(driver, 6).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, ".tencent-captcha-dy__verify-confirm-btn"))
        )
        log.info("[captcha] confirm class=%r displayed=%s enabled_attr=%s",
                 (confirm.get_attribute("class") or "")[-70:], confirm.is_displayed(),
                 confirm.get_attribute("disabled"))
        WebDriverWait(driver, 6).until(
            lambda _d: "disabled" not in (confirm.get_attribute("class") or "")
        )
        driver.execute_script("arguments[0].click();", confirm)
    except Exception as exc:
        log.error("[captcha] confirm button failed: %s", exc)
        return False
    # The login response (bizrt.token / bizrt.userInfo) is parsed only after the captcha clears,
    # so patch this document now - waiting until harvest_routes would miss it entirely.
    try:
        driver.execute_script(HARVEST_JS)
    except Exception as exc:
        log.warning("[captcha] could not pre-arm the capture hook: %s", exc)
    # 95598 needs ~10s after confirm to finish the redirect; a fixed short sleep used to
    # report solved=False on runs that had actually passed (09:48:35 False vs 09:48:44 loggedIn).
    deadline = time.time() + 20
    while time.time() < deadline:
        st = page_state(driver)
        if st["loggedIn"] or not st["on_login"]:
            return True
        time.sleep(1.0)
    return False


def harvest(driver) -> list[dict]:
    try:
        return driver.execute_script("return window.__apiResponses || [];") or []
    except Exception:
        return []


def browse_and_collect(driver, wait: int = 20) -> list[dict]:
    """Let the SPA load the data pages itself; its interceptor decrypts, our JSON.parse hook
    records. This is how we get data without ever recovering the session key."""
    driver.execute_script("window.__apiResponses = [];")
    try:
        driver.get(HOME_URL)
    except Exception:
        pass
    time.sleep(wait)
    out = harvest(driver)
    for route in ("/osgweb/electricityUsageInquiry", "/osgweb/myElectricityBills"):
        try:
            driver.execute_script(
                "const vm=document.getElementById('app').__vue__;"
                "try{ vm.$router.push(arguments[0]); }catch(e){}", route)
            time.sleep(8)
            out += harvest(driver)
        except Exception as exc:
            log.info("route %s failed: %s", route, exc)
    return out


def diff_against_har(captured: list[dict], har_path: str) -> None:
    """Compare our would-be f06 against a known-good login capture: header names and body keys."""
    try:
        har = json.load(open(har_path, encoding="utf-8"))
    except Exception as exc:
        log.error("cannot read HAR %s: %s", har_path, exc)
        return
    good = None
    for en in har["log"]["entries"]:
        if "c44/f06" in en["request"]["url"]:
            hdrs = {h["name"]: h["value"] for h in en["request"]["headers"]}
            body = (en["request"].get("postData") or {}).get("text", "")
            try:
                bk = sorted(json.loads(body).keys())
            except Exception:
                bk = None
            good = {"headerNames": sorted(k.lower() for k in hdrs),
                    "bodyKeys": bk, "bodyLen": len(body),
                    "keyCodeLen": len(hdrs.get("keyCode", "")),
                    "deviceTokenTXLen": len(hdrs.get("deviceTokenTX", ""))}
            break
    if not good:
        log.error("no c44/f06 entry in that HAR")
        return
    if not captured:
        log.warning("interceptor caught nothing (submit may not have fired f06)")
        return
    mine = captured[-1]
    mine_lower = sorted(h.lower() for h in mine["headerNames"])
    print("\n=== f06 diff: ours vs known-good HAR ===")
    print("  headers only in the GOOD request:",
          [h for h in good["headerNames"] if h not in mine_lower] or "none")
    print("  headers only in OURS:",
          [h for h in mine_lower if h not in good["headerNames"]] or "none")
    print(f"  bodyKeys  good={good['bodyKeys']}  ours={mine['bodyKeys']}")
    print(f"  bodyLen   good={good['bodyLen']}  ours={mine['bodyLen']}")
    print(f"  keyCode len good={good['keyCodeLen']} ours={mine['keyCodeLen']}"
          f"   deviceTokenTX len good={good['deviceTokenTXLen']} ours={mine['deviceTokenTXLen']}")
    print(f"  our flags: Authorization={mine['hasAuthorization']} sessionId={mine['hasSessionId']}"
          f" retryCount={mine['hasRetryCount']} t={mine['hasT']} timestamp={mine['hasTimestamp']}")
    print(f"  our cookies: {mine['cookieNames']}")
    print("  (requests were aborted client-side; 95598 never received this f06)")


def wait_for_human_login(driver, minutes: int) -> bool:
    """Poll until a human finishes logging in in this same browser profile."""
    deadline = time.time() + minutes * 60
    last = None
    while time.time() < deadline:
        st = page_state(driver)
        if st["loggedIn"] or not st["on_login"]:
            return True
        if st["blocked"] != last:
            log.info("page currently shows: %s", st["blocked"] or "(no error)")
            last = st["blocked"]
        time.sleep(5)
    return False


def watch_ttl(driver, minutes: int, max_hours: float, csv_path: Path) -> None:
    """Poll the logged-in session until it dies, so TTL is measured, not guessed.

    This is the number that decides the whole architecture: if a session lasts days, the
    browser only has to log in occasionally; if it lasts hours, an unattended browser login
    (and therefore the captcha solver) is mandatory.

    The probe must not reload the document: rendering /login deletes localStorage.token, so
    a page-load probe logs itself out. We move with the app's router and read the API codes.
    """
    import csv
    deadline = time.time() + max_hours * 3600
    new = not csv_path.exists()
    seen_plain = 0
    with csv_path.open("a", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        if new:
            w.writerow(["checked_at", "state", "url", "note"])
        while time.time() < deadline:
            _push_route(driver, PROBE_ROUTE)
            time.sleep(6)
            st = page_state(driver)
            recs = driver.execute_script("return window.__apiResponses || [];") or []
            errs = sorted({str((r.get("value") or {}).get("message"))[:16]
                           for r in recs if (r.get("value") or {}).get("code") not in (1, "1", None)})
            if st["on_login"]:
                state, note = "dead", "bounced to /login"
            elif has_business_data(recs):
                state, note = "alive", f"{sum(1 for r in recs if is_business(r))} data payloads"
            else:
                state, note = "degraded", ("no data; " + ";".join(errs)) if errs else "no data"
            w.writerow([time.strftime("%Y-%m-%d %H:%M:%S"), state, driver.current_url[:60], note])
            fh.flush()
            log.info("TTL probe: %s (%s)", state, note)
            plain = driver.execute_script("return window.__plain || [];") or []
            for p in plain[seen_plain:]:
                log.info("  auth-shaped object on %s: %s", p.get("href"), ",".join(p.get("shape") or []))
            seen_plain = len(plain)
            if state == "dead":
                log.info("session died; TTL recorded in %s", csv_path)
                return
            time.sleep(max(0, minutes * 60 - 6))


ROUTE_RE = re.compile(r"(ele|usage|day|month|year|bill|ladder|balance|amount|qgl|zdy|zhfw)", re.I)
# A page whose data request needs the bearer token: 'alive' here means the session really works,
# not just that the router guard let us through.
PROBE_ROUTE = "/electricitySummary"


def list_routes(driver) -> list[str]:
    """Read the SPA's own router table instead of guessing URLs."""
    js = r"""
    const vm = document.getElementById('app').__vue__;
    const out = [];
    try { (vm.$router.getRoutes ? vm.$router.getRoutes() : []).forEach(r => r.path && out.push(r.path)); } catch (e) {}
    if (!out.length) {
      const walk = (rs, base) => (rs || []).forEach(r => {
        if (!r || !r.path) return;
        const full = r.path.startsWith('/') ? r.path : base + '/' + r.path;
        out.push(full);
        if (r.children) walk(r.children, full);
      });
      try { walk(vm.$router.options.routes, ''); } catch (e) {}
    }
    return [...new Set(out)];
    """
    try:
        return driver.execute_script(js) or []
    except Exception as exc:
        log.info("route enumeration failed: %s", exc)
        return []


def export_browser_auth(driver, out: Path) -> dict:
    """Rebuild the plaintext OAuth material the app splits across header and body.

    The app keeps only encrypted blobs in localStorage and the Vue store, but every request
    carries the first half of the bearer in Authorization/t and the second half in the
    pre-encryption body (_access_token/_t). Concatenating the two gives what data_client wants.
    Logs names and lengths only, never values.
    """
    try:
        got = driver.execute_script(
            "const out = {auth: window.__auth || {hdr: [], body: []},"
            "             resp: (window.__apiResponses || []).slice(-60),"
            "             login: {token: null, userInfo: null, access_token: null}};"
            "for (const r of (window.__apiResponses || [])) {"
            "  const v = r && r.value; if (!v || typeof v !== 'object') continue;"
            "  const d = v.data;"
            "  if (d && typeof d === 'object' && d.bizrt) {"
            "    if (!out.login.token && d.bizrt.token) out.login.token = d.bizrt.token;"
            "    if (!out.login.userInfo && Array.isArray(d.bizrt.userInfo) && d.bizrt.userInfo.length)"
            "      out.login.userInfo = d.bizrt.userInfo[0];"
            "  }"
            "  const at = v.access_token || (d && d.access_token) || (d && d.data && d.data.access_token);"
            "  if (!out.login.access_token && at) out.login.access_token = at;"
            "}"
            "return out;") or {}
    except Exception as exc:
        log.error("[auth] capture read failed: %s", exc)
        return {}
    hdr = {h["k"]: h["v"] for h in got.get("auth", {}).get("hdr", []) if h.get("k")}
    bodies = got.get("auth", {}).get("body", [])
    body = bodies[-1] if bodies else {}

    bearer = (hdr.get("Authorization") or "")
    bearer = bearer[7:] if bearer.startswith("Bearer ") else bearer
    access = bearer + (body.get("access_tail") or "")
    token = (hdr.get("t") or "") + (body.get("t_tail") or "")

    def find_user_id(node, depth=0):
        if depth > 6:
            return None
        if isinstance(node, dict):
            if any(k.lower() in ("userid", "user_id") for k in node):
                return node
            for v in node.values():
                r = find_user_id(v, depth + 1)
                if r:
                    return r
        elif isinstance(node, list):
            for v in node:
                r = find_user_id(v, depth + 1)
                if r:
                    return r
        elif isinstance(node, str) and node.startswith("{"):
            try:
                return find_user_id(json.loads(node), depth + 1)
            except Exception:
                return None
        return None

    user_info = None
    for rec in got.get("resp", []):
        user_info = find_user_id((rec or {}).get("value")) or user_info

    # Values the app itself parsed beat anything reconstructed from the wire: the login response
    # carries the full token/userInfo, and getWebToken the full access_token.
    login = got.get("login") or {}
    if login.get("access_token"):
        access = login["access_token"]
    if login.get("token"):
        token = login["token"]
    if login.get("userInfo"):
        user_info = login["userInfo"]
    log.info("[auth] source: %s", "app-parsed" if login.get("token") else "header halves")

    payload = {"accessToken": access, "token": token, "userInfo": user_info,
               "source": {"access_full": bool(login.get("access_token")),
                          "token_full": bool(login.get("token")),
                          "app_parsed": bool(login.get("token") and login.get("access_token")),
                          "bearer_head": len(bearer), "access_tail": len(body.get("access_tail") or ""),
                          "t_head": len(hdr.get("t") or ""), "bodies": len(bodies)}}
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    out.chmod(0o600)
    log.info("[auth] accessToken=%d (head %d + tail %d) token=%d userInfo=%s -> %s",
             len(access), payload["source"]["bearer_head"], payload["source"]["access_tail"],
             len(token), "yes" if user_info else "NOT FOUND", out)
    return payload


def harvest_routes(driver, max_routes: int = 12, wait: int = 7, reload: bool = True,
                   routes_out: Path | None = None, only: list[str] | None = None) -> dict:
    """Walk the app's own pages with the existing session and collect decrypted payloads."""
    # The capture hook must NOT be installed before login: it breaks tdc.js writing
    # TDC_itoken, and f06 without that cookie is hard-rejected as RK001. Attach it here instead
    # (HARVEST_JS self-guards, so re-attaching is free) now that the captcha is behind us.
    if reload:
        # Register the hook for future documents rather than patching this one: the SPA
        # captured its own XMLHttpRequest reference at boot, so patching after load sees
        # requests but never responses. Doing this only now (post-login) keeps it from
        # breaking tdc.js/TDC_itoken during the captcha.
        try:
            driver.execute_cdp_cmd("Page.addScriptToEvaluateOnNewDocument", {"source": HARVEST_JS})
        except Exception as exc:
            log.warning("could not register the capture hook for new documents: %s", exc)
        driver.get(HOME_URL)
        time.sleep(8)
    try:
        driver.execute_script(HARVEST_JS)
    except Exception as exc:
        log.warning("could not attach the capture hook after login: %s", exc)
    allr = list_routes(driver)
    if routes_out and allr:
        routes_out.write_text(json.dumps(allr, ensure_ascii=False, indent=0), encoding="utf-8")
    picked = only if only else [r for r in allr if ROUTE_RE.search(r)]
    picked = picked[:max_routes]
    log.info("router has %d routes; visiting %s", len(allr), picked)
    result: dict = {}
    for route in picked:
        driver.execute_script("window.__apiResponses = []; window.__lastApiUrl = null;")
        _push_route(driver, "/my95598")          # start each page from home
        driver.execute_script("window.__apiResponses = []; window.__lastApiUrl = null;")
        _push_route(driver, route)
        # Pages retry their first request after a GC117(消息异常) by fetching a fresh keyCode,
        # so a fixed sleep used to capture only the key exchange and report an empty page.
        deadline = time.time() + wait
        recs: list = []
        while time.time() < deadline:
            time.sleep(2)
            recs = driver.execute_script("return window.__apiResponses || [];") or []
            if has_business_data(recs):
                break
        if recs:
            result[route] = recs
            log.info("  %-40s -> %d payloads, %d with data",
                     route[:40], len(recs), sum(1 for r in recs if is_business(r)))
        else:
            log.info("  %-40s -> nothing", route[:40])
    return result


def _push_route(driver, route: str) -> None:
    driver.execute_script(
        "const vm=document.getElementById('app') && document.getElementById('app').__vue__;"
        "if(vm&&vm.$router){try{vm.$router.push(arguments[0])}catch(e){}}", route)


METER_ECHO_JS = r"""
const m = (document.body.innerText || '').match(/用电户号[:：]\s*(\d{6,})/);
return m ? m[1] : null;
"""

METER_OPEN_JS = r"""
// The picker lives in the header block of the usage pages; Element UI mounts its dropdown
// into body and toggles it on click, so callers must confirm it actually opened.
const box = [...document.querySelectorAll('.el-select')]
  .find(e => { const r = e.getBoundingClientRect(); return r.width > 60 && r.top > 300 && r.top < 600; });
if (!box) return {error: 'no meter select on page'};
box.click();
return {ok: true};
"""

METER_COUNT_JS = r"""
return [...document.querySelectorAll('.el-select-dropdown__item')]
  .filter(e => e.getBoundingClientRect().height > 4).length;
"""

METER_PICK_JS = r"""
const i = arguments[0];
const items = [...document.querySelectorAll('.el-select-dropdown__item')]
  .filter(e => e.getBoundingClientRect().height > 4);
if (i >= items.length) return {error: 'index out of range', n: items.length};
const t = (items[i].innerText || '').trim().slice(0, 40);
items[i].click();
return {ok: true, text: t};
"""


def payload_shape(rec: dict) -> str | None:
    """Name a business payload by what it contains, because the captured URL is unreliable:
    responses are recorded at JSON.parse time with no request context, and one gateway path
    (c9/f02) carries several unrelated payloads."""
    v = (rec or {}).get("value") or {}
    if not isinstance(v, dict):
        return None
    # Some endpoints hand back the business object without a {code,data} envelope.
    if "skey" in v and isinstance(v.get("data"), str):
        return None                      # encrypted envelope, never decrypted by us
    for key, name in (("billRead", "ladder"), ("pointList", "ladder"), ("readList", "ladder")):
        if key in v:
            return name
    d = v.get("data")
    if isinstance(d, str):
        try:
            d = json.loads(d)
        except Exception:
            d = None
    if not isinstance(d, dict):
        # No envelope: name it by its own keys so an unfamiliar shape is reported, not dropped.
        return "other:" + "|".join(sorted(v)[:4]) if v else None
    for key in ("billRead", "pointList", "readList"):
        if key in d:
            return "ladder"
    # 余额藏在 data.list[0] 里，键名和别页的 list 撞车，所以必须认字段不能认键名
    lst = d.get("list")
    if isinstance(lst, list) and lst and isinstance(lst[0], dict) and any(
            k in lst[0] for k in ("estiAmt", "prepayBal", "historyOwe")):
        return "balance"
    for key, name in (("sevenEleList", "daily_ele"), ("mothEleList", "monthly_ele"),
                      ("powerUserList", "meter_list")):
        if key in d:
            return name
    b = d.get("bizrt")
    if isinstance(b, dict) and "powerUserList" in b:
        return "meter_list"
    # Name anything else by its own keys: which payloads follow a meter switch is still unknown,
    # and an unrecognised shape must show up in the report rather than be silently dropped.
    return "other:" + "|".join(sorted(d)[:4])


CLICK_TEXT_JS = r"""
// 页面上的「近7天/近30天」「日用电量」这类切换条没有稳定选择器（radio/tab/裸 span 都可能），
// 按可见文本点最深的那个元素：外层容器点了不生效，标签本身才绑事件。
const want = arguments[0];
const all = [...document.querySelectorAll('.el-radio,.el-radio__label,.el-checkbox,.el-checkbox__label,'
                                          + 'li,button,span,div,a')];
const hit = all.filter(e => (e.innerText || '').trim() === want
                        && e.getBoundingClientRect().width > 0);
if (!hit.length) return {error: 'no such control', want: want,
                         seen: all.map(e => (e.innerText || '').trim())
                                  .filter(t => /近\\d+天|日用电量|月度电费/.test(t)).slice(0, 8)};
hit[hit.length - 1].click();
return {ok: true, n: hit.length};
"""


def _digest(payload) -> str:
    return hashlib.sha1(json.dumps(payload, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:12]


# 账号级载荷：本来就只有一份，逐表相同不是串号
ACCOUNT_SHAPES = {"meter_list"}


def harvest_by_meter(driver, pages: list[str], out: Path, wait: int = 18,
                     set_window=None) -> dict:
    """Collect business payloads per meter, attributing each by the 户号 the page echoes back.

    Attribution is self-verified rather than inferred: after selecting an option we read the
    plaintext 用电户号 the page prints, and key everything collected under it. That is deliberate
    - this integration previously shipped a meter cross-wiring bug caused by assuming order.

    The echo alone is not enough: on some pages picking another meter repaints the header but
    refetches nothing, so the buffer still holds the previous meter's response. We hash each
    payload and refuse to file an exact duplicate under a second 户号 - 09-29 a run did exactly
    that and wrote the same 80.28 series under both meters.

    set_window(driver) runs once per page load and once after every meter pick. Whether a
    meter switch resets the 近7天/近30天 choice on its own wasn't isolated; re-asserting at both
    points costs one click and covers both answers. 09-30 run: both meters came back with 31
    rows, so the extra click doesn't collapse a window that already holds.
    """
    collected: dict[str, dict] = {}
    owners: dict[tuple[str, str], str] = {}

    def take(cons: str, payloads: dict, whence: str) -> dict:
        keep: dict = {}
        for shape, value in payloads.items():
            if shape in ACCOUNT_SHAPES:
                keep[shape] = value
                continue
            who = owners.get((shape, _digest(value)))
            if who is not None and who != cons:
                log.warning("[meter] %s 的 %s 与 %s 逐字节相同（%s）：页面没有按选中的表重发，"
                            "归属无法自证，丢弃", cons, shape, who, whence)
                continue
            owners[(shape, _digest(value))] = cons
            keep[shape] = value
        if keep:
            collected.setdefault(cons, {}).update(keep)
            log.info("[meter] %s <- %s (%s)", cons, sorted(keep), whence)
        return keep

    for route in pages:
        _push_route(driver, route)
        time.sleep(8)
        if set_window:
            # 缓冲区按到达顺序排列，dict 推导同形状后者覆盖前者；
            # 先收首屏再点宽窗口，宽窗口若真发回来就顶掉窄的那份（行数以实测为准）
            log.info("[meter] %s 设窗口: %s", route, set_window(driver))
            time.sleep(6)
        recs = driver.execute_script("return window.__apiResponses || [];") or []
        echo = driver.execute_script(METER_ECHO_JS)
        if not echo:
            log.info("[meter] %s has no 用电户号 echo; skipping", route)
            continue
        # The page already fetched for its default meter on load; take that before clearing.
        first = {s: r["value"] for r in recs if (s := payload_shape(r))}
        log.info("[meter] %s default=%s shapes=%s", route, echo, list(first) or "none")
        take(echo, first, f"{route} 首屏")

        driver.execute_script(METER_OPEN_JS)
        time.sleep(1.2)
        n = driver.execute_script(METER_COUNT_JS) or 0
        if not n:
            log.info("[meter] %s exposes no meter dropdown (single-meter page?)", route)
            continue
        for i in range(n):
            for _ in range(3):                      # dropdown toggles and mounts lazily
                if (driver.execute_script(METER_COUNT_JS) or 0) > 0:
                    break
                driver.execute_script(METER_OPEN_JS)
                time.sleep(1.2)
            driver.execute_script("window.__apiResponses = [];")
            picked = driver.execute_script(METER_PICK_JS, i)
            time.sleep(2.5)
            echo = driver.execute_script(METER_ECHO_JS)
            if not echo:
                log.warning("[meter] %s option %s (%s): no echo after pick", route, i, picked)
                continue
            if set_window:
                set_window(driver)      # 换表后的窗口状态没测过，这里重新点一次再说
                time.sleep(1.5)
            deadline = time.time() + wait
            settled: float | None = None
            keep: dict = {}
            while time.time() < deadline:
                time.sleep(2)
                recs = driver.execute_script("return window.__apiResponses || [];") or []
                fresh = {s: r["value"] for r in recs if (s := payload_shape(r))}
                if fresh:
                    keep.update(take(echo, fresh, f"{route} opt{i}"))
                    driver.execute_script("window.__apiResponses = [];")
                    settled = time.time() + 6       # 到齐判定：再静置一轮，收后续到达的表级载荷
                if settled is not None and time.time() >= settled:
                    break
            log.info("[meter] %s option %s -> %s shapes=%s%s", route, i, echo,
                     sorted(keep) or "none", "" if keep else " (no usable refetch)")
    out.write_text(json.dumps(collected, ensure_ascii=False, indent=1), encoding="utf-8")
    log.info("[meter] %d meters x shapes: %s", len(collected),
             {m: sorted(v) for m, v in collected.items()})
    return collected


def build_push_items(collected: dict) -> list[dict]:
    """Flatten {户号: {形状: 响应}} into the list the HA webhook expects.

    Shapes we have no consumer for in the integration stay out of the bundle rather than
    arriving as data HA would have to guess about.
    """
    pushable = ("daily_ele", "monthly_ele", "ladder", "balance", "meter_list")
    items: list[dict] = []
    for cons, shapes in (collected or {}).items():
        for shape in pushable:
            value = shapes.get(shape)
            if isinstance(value, dict):
                items.append({"shape": shape, "consNo": cons, "response": value})
    return items


def push_to_ha(items: list[dict], url: str, token: str, timeout: int = 30) -> dict:
    """POST the harvested bundle to HA's webhook endpoint and return its verdict.

    The webhook token is a credential, so it rides in the URL only, is never logged, and the
    caller must pass it via the environment rather than argv.
    """
    body = json.dumps({
        "pushed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "items": items,
    }).encode()
    target = url.rstrip("/") + "/api/webhook/" + token
    req = urllib.request.Request(target, data=body, method="POST",
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            verdict = json.loads(resp.read().decode())
    except Exception as exc:
        # urlopen 的报错会把完整 URL 带进来，而 token 就写在 URL 里
        text = str(exc).replace(token, "***")
        log.error("push failed: %s", text)
        return {"ok": False, "error": text}
    log.info("pushed %d payloads -> HA stored=%s skipped=%s",
             len(items), verdict.get("stored"), (verdict.get("meta") or {}).get("skipped"))
    return verdict


def push_harvest(collected: dict) -> dict:
    """Push the per-meter bundle using HA_WEBHOOK_URL / HA_WEBHOOK_TOKEN (env only)."""
    url = os.environ.get("HA_WEBHOOK_URL", "")
    token = os.environ.get("HA_WEBHOOK_TOKEN", "")
    if not url or not token:
        log.error("--push 需要环境变量 HA_WEBHOOK_URL 与 HA_WEBHOOK_TOKEN，当前缺少其一")
        return {"ok": False}
    items = build_push_items(collected)
    if not items:
        log.error("--push：逐表取数没拿到任何可推送载荷，不推空包（HA 会把旧缓存整包换掉）")
        return {"ok": False}
    return push_to_ha(items, url, token)


def is_business(rec) -> bool:
    v = (rec or {}).get("value") or {}
    d = v.get("data")
    return (v.get("code") in (1, "1") and bool(d) and isinstance(d, (dict, list))
            and not (isinstance(d, dict) and "keyCode" in d))


def has_business_data(recs) -> bool:
    return any(is_business(r) for r in recs)


def save_session(driver, path: Path) -> dict:
    """Persist cookies + localStorage. JSESSIONID and the 瑞数 cookie are non-persistent
    session cookies, so Chrome never writes them to disk: without this, any browser restart
    silently destroys the login even though TDC_itoken survives for a year."""
    data = {
        "saved_at": time.strftime("%Y-%m-%d %H:%M:%S"),
        "cookies": driver.get_cookies(),
        "local": driver.execute_script(
            "const o={};for(let i=0;i<localStorage.length;i++){const k=localStorage.key(i);"
            "o[k]=localStorage.getItem(k);}return o;"),
        # requestCyu (the client SM2 keypair) and deviceToken live in sessionStorage, which dies
        # with the tab. Without them a restored session sends no Authorization and every member
        # API answers GC117.
        "session": driver.execute_script(
            "const o={};for(let i=0;i<sessionStorage.length;i++){const k=sessionStorage.key(i);"
            "if(k!=='__hlog')o[k]=sessionStorage.getItem(k);}return o;"),
    }
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except Exception:
        pass
    names = sorted(c.get("name", "?") for c in data["cookies"])
    log.info("session saved to %s (cookies: %s)", path, ", ".join(names))
    return {"cookieCount": len(data["cookies"]), "names": names}


def restore_session(driver, path: Path) -> bool:
    if not path.exists():
        return False
    data = json.loads(path.read_text(encoding="utf-8"))
    cookies = list(data.get("cookies") or [])
    driver.get(BASE + "/osgweb/login")
    time.sleep(3)
    ok = 0
    for ck in cookies:
        c = {k: v for k, v in ck.items() if k in ("name", "value", "domain", "path", "secure", "sameSite")}
        if c.get("sameSite") not in ("Strict", "Lax", "None"):
            c.pop("sameSite", None)
        c.pop("expiry", None)
        try:
            # Without this the jar ends up with two JSESSIONID / two 瑞数 cookies (ours plus
            # whatever the fresh browser context minted), and the server then sees a stale one.
            driver.delete_cookie(c["name"])
            driver.add_cookie(c)
            ok += 1
        except Exception:
            pass
    inject_local(driver, data.get("local"))
    inject_local(driver, data.get("session"), "sessionStorage")
    log.info("restored %d cookies (saved %s): %s", ok, data.get("saved_at"),
             ", ".join(sorted(c.get("name", "?") for c in cookies)) or "-")
    return ok > 0


def inject_local(driver, mapping: dict, kind: str = "localStorage") -> None:
    for k, v in (mapping or {}).items():
        try:
            driver.execute_script(
                f"try{{{kind}.setItem(arguments[0],arguments[1])}}catch(e){{}}", k, v)
        except Exception:
            pass


def has_token(driver) -> str:
    return str(driver.execute_script(
        "const t=localStorage.getItem('token'); return t?String(t).length+' chars':'(absent)'"))


def audit_token_state(driver) -> None:
    """Where does the app keep its OAuth bearer token? A restored session passes the route guard
    but every member API answers GC117(消息异常), which looks like an envelope with an empty
    _access_token. Logs paths and lengths only, never the token itself."""
    js = r"""
    const out = {session_keys: {}, local_keys: {}, bearer_paths: [], store: []};
    for (let i = 0; i < sessionStorage.length; i++) {
      const k = sessionStorage.key(i); out.session_keys[k] = String(sessionStorage.getItem(k)).length;
    }
    for (let i = 0; i < localStorage.length; i++) {
      const k = localStorage.key(i); out.local_keys[k] = String(localStorage.getItem(k)).length;
    }
    const vm = document.getElementById('app') && document.getElementById('app').__vue__;
    const seen = new Set();
    const walk = (o, path, depth) => {
      if (o == null || depth > 6 || seen.has(o)) return;
      if (typeof o === 'object') seen.add(o);
      if (typeof o === 'string') {
        if (o.startsWith('WEB.') || (o.split('.').length === 3 && o.length > 80)) {
          out.bearer_paths.push({path, len: o.length});
        }
        return;
      }
      if (typeof o !== 'object') return;
      for (const k of Object.keys(o)) {
        if (k === '__proto__') continue;
        walk(o[k], path + '.' + k, depth + 1);
      }
    };
    if (vm) {
      walk(vm.$store && vm.$store.state, 'store', 0);
      if (vm.$configuration) out.config_keys = Object.keys(vm.$configuration).slice(0, 40);
      if (vm.$api) out.api_keys = Object.keys(vm.$api).slice(0, 60);
      out.store_modules = vm.$store && vm.$store._modules ? Object.keys(vm.$store._modules.root._children) : null;
    }
    out.cookie_names = document.cookie.split(';').map(s => s.trim().split('=')[0]).filter(Boolean);
    return out;
    """
    try:
        a = driver.execute_script(js) or {}
    except Exception as exc:
        log.info("  audit failed: %s", exc)
        return
    log.info("  sessionStorage: %s", a.get("session_keys"))
    log.info("  cookies: %s", a.get("cookie_names"))
    log.info("  bearer found at: %s", a.get("bearer_paths") or "NOWHERE (no access_token in memory)")
    log.info("  store modules: %s", a.get("store_modules"))
    api = a.get("api_keys") or []
    if api:
        log.info("  $api (%d keys): %s", len(api), ", ".join(api[:40]))


def _region_bytes(driver, x: float, y: float, w: float, h: float) -> bytes:
    """Pixels of a viewport region, so 'did the tick change' is answered by what is drawn rather
    than by a class name on a 0x0 leftover node."""
    img = Image.open(io.BytesIO(driver.get_screenshot_as_png()))
    scale = img.width / max(1.0, float(driver.execute_script("return window.innerWidth") or img.width))
    x0, y0 = max(0, int(x * scale)), max(0, int(y * scale))
    x1, y1 = x0 + max(4, int(w * scale)), y0 + max(4, int(h * scale))
    return img.crop((x0, y0, min(img.width, x1), min(img.height, y1))).tobytes()


def _tick_box(driver):
    """The visible 16x16 tick. The card carries two pairs of .checked-box (password tab and SMS
    tab), and the inactive pair measures 0x0 -- picking by index grabs a decoy whose class flips
    while nothing on screen changes."""
    for sel in (".checked-box.un-checked", ".checked-box"):
        for el in driver.find_elements(By.CSS_SELECTOR, sel):
            r = rect_of(driver, el)
            if r and r[2] >= 8 and r[3] >= 8:
                return el, r
    return None, None


def ensure_agreement(driver) -> dict:
    """Tick the agreement and prove it by pixels of the tick's own box."""
    st = {"checked": False, "tries": []}
    for _attempt in range(3):
        el, r = _tick_box(driver)
        if el is None:
            st["reason"] = "no visible tick box (>=8px) found"
            return st
        cx, cy = r[0] + r[2] / 2, r[1] + r[3] / 2
        region = (cx - r[2], cy - r[3], r[2] * 3, r[3] * 3)
        before = _region_bytes(driver, *region)
        # el.click() is a real pointer event and re-scrolls the element into view first; the
        # ActionChains path uses a rect measured before earlier typing scrolled the page, which is
        # why it kept missing this 16x16 target while dispatched events at the live coordinate
        # flipped it.
        try:
            el.click()
            how = "el"
        except Exception:
            how = "js"
        time.sleep(random.uniform(0.6, 0.9))
        if _region_bytes(driver, *region) == before:
            live = driver.execute_script(
                """const e=document.querySelector('.checked-box.un-checked');
                   const r=e.getBoundingClientRect();
                   return [r.left + r.width/2, r.top + r.height/2];""")
            if live and live[0] > 0:
                driver.execute_script(
                    """const e=document.elementFromPoint(arguments[0],arguments[1]);
                    if(e){['mousedown','mouseup','click'].forEach(t=>e.dispatchEvent(
                        new MouseEvent(t,{bubbles:true,cancelable:true,
                                          clientX:arguments[0],clientY:arguments[1]})));}""",
                    live[0], live[1])
                how += "+dispatch"
                time.sleep(random.uniform(0.6, 0.9))
        st["tries"].append(f"{how}@{cx:.0f},{cy:.0f}")
        if _region_bytes(driver, *region) != before:
            st.update(checked=True, point=[cx, cy])
            return st
    return st


def dump_login_state(driver, tag: str = "state", shot: Path | None = None) -> None:
    """What is actually on the page after we submit. 'timeout' can mean a captcha we failed to
    detect, an SMS step, or an account-lock notice, and those need different fixes."""
    info = driver.execute_script(
        """const txt=(document.body&&document.body.innerText||'').replace(/\\s+/g,' ').trim();
        const hits={};
        for (const el of document.querySelectorAll('*')) {
          const cls=(typeof el.className==='string'?el.className:'');
          const m=cls.match(/(tencent-captcha[^\\s]*|tc-[^\\s]*|captcha[^\\s]*|verify[^\\s]*|slide[^\\s]*)/i);
          if (m) hits[m[1]]=(hits[m[1]]||0)+1;
        }
        return {text: txt.slice(0,420), len: txt.length,
                iframes: [...document.querySelectorAll('iframe,frame')].map(f=>{
                  const r=f.getBoundingClientRect();
                  return {src:(f.src||'').slice(0,80), w:Math.round(r.width), h:Math.round(r.height)};}),
                classHits: Object.entries(hits).sort((a,b)=>b[1]-a[1]).slice(0,12)};""") or {}
    log.info("[%s] url=%s", tag, driver.current_url)
    log.info("[%s] pageText(%d chars): %s", tag, info.get("len") or 0,
             (info.get("text") or "")[:380])
    log.info("[%s] iframes: %s", tag, info.get("iframes"))
    log.info("[%s] captcha-ish classes: %s", tag, info.get("classHits"))
    if shot:
        try:
            shot.parent.mkdir(parents=True, exist_ok=True)
            driver.save_screenshot(str(shot))
            log.info("[%s] screenshot -> %s", tag, shot)
        except Exception as exc:
            log.info("[%s] screenshot failed: %s", tag, type(exc).__name__)


def probe_restore(driver, sess: Path) -> bool:
    """Re-inject a saved session and check it properly: past the route guard AND able to fetch
    data. A bounce to /login is only half the story — the guard reads localStorage.token, while
    the member APIs need the bearer token that the app gets from oauth/authorize + getWebToken.

    Never delete_all_cookies() here: selenium maps it to CDP Network.clearBrowserCookies, which
    wipes the whole profile jar. Doing that cost this profile its TDC_itoken."""
    data = json.loads(sess.read_text(encoding="utf-8")) or {}
    restore_session(driver, sess)
    driver.get(HOME_URL)
    time.sleep(9)
    st = page_state(driver)
    log.info("  [reload] url=%s on_login=%s token=%s", st["url"], st["on_login"], has_token(driver))
    dump_api_summary(driver)
    if not st["on_login"] and _session_serves_data(driver):
        log.info("  [reload] ACCEPTED with working data")
        return True

    # The login document's own boot code deletes localStorage.token (that is why the previous
    # line prints '(absent)'), so a full-page reload cannot carry a session. Move with the app's
    # router instead: same document, so the storage written here still counts.
    inject_local(driver, data.get("local"))
    inject_local(driver, data.get("session"), "sessionStorage")
    log.info("  [push] token re-injected=%s", has_token(driver))
    _push_route(driver, "/my95598")
    time.sleep(11)
    st2 = page_state(driver)
    log.info("  [push] url=%s on_login=%s token=%s", st2["url"], st2["on_login"], has_token(driver))
    dump_api_summary(driver)
    if not st2["on_login"]:
        log.info("  [push] guard passed; checking whether data actually loads")
        audit_token_state(driver)
        return _session_serves_data(driver)
    return False


def _session_serves_data(driver, tries: int = 2) -> bool:
    """Push the probe page and look for a real data payload, not just a key exchange."""
    for _ in range(tries):
        driver.execute_script("window.__apiResponses = [];")
        _push_route(driver, PROBE_ROUTE)
        time.sleep(8)
        recs = driver.execute_script("return window.__apiResponses || [];") or []
        if has_business_data(recs):
            log.info("  probe page served %d data payloads", sum(1 for r in recs if is_business(r)))
            return True
        log.info("  probe page served no data (%d payloads, errors: %s)", len(recs),
                 ";".join(sorted({str((r.get('value') or {}).get('message'))[:14] for r in recs
                                  if (r.get('value') or {}).get('code') not in (1, '1', None)}) or "-"))
    return False


def save_api_inventory(driver, out: Path) -> None:
    """Distinct /api/ paths the app actually called during this walk, with the page that
    called them — the route-to-endpoint map we need to fetch data directly."""
    rows = driver.execute_script(
        """const a = JSON.parse(sessionStorage.getItem('__hlog') || '[]');
        const seen = {};
        for (const r of a) if (r.req) {
          const k = r.req + '|' + r.href;
          if (!seen[k]) seen[k] = {api:r.req, page:r.href, m:r.m, h:r.h || [], n:0};
          seen[k].n++;
        }
        return Object.values(seen);""") or []
    out.write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
    for r in rows:
        log.info("  %-5s %-46s page=%-26s hdrs=%s", r.get("m"), (r.get("api") or "")[-46:],
                 str(r.get("page"))[-26:], ",".join(r.get("h") or []))
    log.info("api inventory: %d distinct calls -> %s", len(rows), out)


def dump_api_summary(driver) -> None:
    """Log each distinct API the page called and the code it got back: that separates a
    server-side 'not logged in' answer from a purely client-side route guard. Reads the
    sessionStorage sink so calls made before a redirect are still visible."""
    recs = driver.execute_script(
        """const a = JSON.parse(sessionStorage.getItem('__hlog') || '[]');
        const seen = {};
        for (const r of a) {
          const k = (r.href||'-') + '|' + (r.url||'-') + '|' + r.code + '|' + r.errcode;
          if (!seen[k]) seen[k] = {href:r.href, url:r.url, code:r.code, msg:r.msg,
                                   errcode:r.errcode, dkeys:r.dkeys, n:0};
          seen[k].n++;
        }
        return Object.values(seen).slice(-14);""") or []
    for r in recs:
        log.info("  api %-18s %s -> code=%s err=%s msg=%s x%s data=%s",
                 str(r.get("href"))[:18], (r.get("url") or "-")[-26:], r.get("code"),
                 r.get("errcode"), str(r.get("msg"))[:24], r.get("n"), r.get("dkeys"))
    log.info("  localStorage: %s",
             ",".join(driver.execute_script("return Object.keys(localStorage)") or []))


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--account", required=False, default="")
    ap.add_argument("--profile", default="./chrome-profile",
                    help="persistent Chrome profile dir; this IS the captcha/WAF identity")
    ap.add_argument("--headed", action="store_true", help="show the window (debugging)")
    ap.add_argument("--warm", action="store_true",
                    help="pre-instantiate the Tencent widget before submitting (off by default: "
                         "see the note in the login path about TDC_itoken)")
    ap.add_argument("--proxy", default=os.environ.get("SGCC_PROXY", ""),
                    help="route Chrome through a proxy, e.g. socks5://172.16.1.7:1081 (DNS is "
                         "forced through it too, so the site cannot see the real egress)")
    ap.add_argument("--check", action="store_true",
                    help="load page + warm identity only; submits no login attempt")
    ap.add_argument("--intercept-f06", action="store_true",
                    help="abort the login POST in the browser and diff our would-be request "
                         "against a known-good HAR; 95598 never receives it, so this costs no "
                         "login attempt")
    ap.add_argument("--har", default=os.environ.get("SGCC_HAR", ""),
                    help="known-good capture to diff against (.har)")
    ap.add_argument("--manual", action="store_true",
                    help="open a visible window and wait for YOU to log in; then harvest")
    ap.add_argument("--wait-minutes", type=int, default=8)
    ap.add_argument("--save-session", default="sgcc_session.json",
                    help="where to persist cookies+localStorage ('' to disable)")
    ap.add_argument("--restore", action="store_true",
                    help="re-inject the saved session instead of logging in")
    ap.add_argument("--harvest-only", action="store_true",
                    help="reuse the profile's live session, walk the app's pages, dump data")
    ap.add_argument("--routes", default="",
                    help="comma-separated SPA routes to visit instead of the keyword guess")
    ap.add_argument("--watch-only", action="store_true",
                    help="no login at all: reuse the profile's existing session and just "
                         "measure how long it survives")
    ap.add_argument("--watch-ttl", action="store_true",
                    help="after harvesting, keep probing the session until it dies")
    ap.add_argument("--ttl-interval-min", type=int, default=10)
    ap.add_argument("--ttl-max-hours", type=float, default=60.0)
    ap.add_argument("--json-out", default="sgcc_data.json")
    ap.add_argument("--captcha", choices=["solver", "agent", "llm", "off"], default="solver",
                    help="how to answer a point-click challenge: vendored CV solver, "
                         "file handshake with an outside vision model (agent), the "
                         "integration's LLM solver (llm), or don't try")
    ap.add_argument("--by-meter", default="", metavar="ROUTES",
                    help="comma-separated pages to walk once per bound meter, attributing every "
                         "payload by the 用电户号 the page echoes (e.g. /my95598,/electricityCharge)")
    ap.add_argument("--agent-timeout", type=int, default=300,
                    help="seconds to wait for the agent's reply in --captcha agent mode")
    ap.add_argument("--push", action="store_true",
                    help="POST the per-meter bundle to the Home Assistant webhook; the URL comes "
                         "from HA_WEBHOOK_URL (e.g. http://172.16.1.x:8123) and the webhook token "
                         "from HA_WEBHOOK_TOKEN — env only, never argv, never logged")
    args = ap.parse_args()

    if args.push and not args.by_meter:
        ap.error("--push 只推逐表取数的结果：请加 --by-meter <页面>，否则没有归属可信的数据可推")

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    if not args.account and not (args.check or args.watch_only or args.harvest_only
                                 or args.manual):
        log.error("--account is required unless --check/--manual/--watch-only/--harvest-only")
        return 2
    password = os.environ.get("SGCC_PASSWORD") or ""
    if not password and not (args.check or args.manual or args.watch_only
                             or args.harvest_only):
        log.error("set SGCC_PASSWORD in the environment (never pass it as an argument)")
        return 2

    driver = make_driver(Path(args.profile),
                         headless=not (args.headed or (args.watch_only and not args.harvest_only)),
                         proxy=args.proxy)
    try:
        if args.proxy:
            # Prove the proxy actually took effect before spending a login attempt on it.
            try:
                driver.get("https://api.ip.sb/geoip")
                j = json.loads(driver.execute_script("return document.body.innerText") or "{}")
                log.info("[--proxy] browser egress: %s / %s %s", j.get("ip"),
                         j.get("country"), j.get("city"))
            except Exception as exc:
                log.error("[--proxy] cannot read browser egress (%s); aborting before login", exc)
                return 7
        sess = Path(args.save_session) if args.save_session else None
        positioned = False
        picked_only = [r.strip() for r in args.routes.split(",") if r.strip()] or None

        if args.restore and sess and sess.exists():
            if not probe_restore(driver, sess):
                log.error("restored session is no longer accepted by 95598")
                return 6
            # probe_restore leaves us on an authenticated /my95598; any driver.get() from here
            # re-runs the login boot code, which deletes localStorage.token and logs us out.
            positioned = True

        if args.harvest_only:
            if not positioned:
                driver.get(HOME_URL)
                time.sleep(8)
            if page_state(driver)["on_login"]:
                log.error("profile has no live session for harvesting")
                return 5
            data = harvest_routes(driver, reload=True,
                                  routes_out=Path(args.json_out + ".routes"),
                                  only=picked_only)
            Path(args.json_out).write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            total = sum(len(v) for v in data.values())
            log.info("harvested %d payloads across %d routes -> %s", total, len(data), args.json_out)
            save_api_inventory(driver, Path(args.json_out + ".api"))
            if args.by_meter:
                collected = harvest_by_meter(
                    driver, [r.strip() for r in args.by_meter.split(",") if r.strip()],
                    Path(args.json_out + ".meters.json"))
                if args.push:
                    push_harvest(collected)
            if sess:
                save_session(driver, sess)
            if args.watch_ttl:
                watch_ttl(driver, minutes=args.ttl_interval_min, max_hours=args.ttl_max_hours,
                          csv_path=Path("ttl_watch.csv"))
            return 0
        if args.watch_only:
            st0 = (driver.get(HOME_URL), time.sleep(8), page_state(driver))[2]
            if st0["on_login"]:
                log.error("profile has no live session; nothing to measure")
                return 5
            log.info("[--watch-only] session is live; measuring TTL every %d min for %.0f h",
                     args.ttl_interval_min, args.ttl_max_hours)
            watch_ttl(driver, minutes=args.ttl_interval_min, max_hours=args.ttl_max_hours,
                      csv_path=Path("ttl_watch.csv"))
            return 0
        open_login(driver)
        log.info("cookies on load: %s", sorted(cookie_names(driver)))
        if args.warm:
            log.info("warm-up (--warm): %s", warm_captcha_identity(driver))
        else:
            # TDC_itoken is written by 95598's own captcha flow on the first-party domain; a
            # hand-instantiated TencentCaptcha never runs that code, so pre-warming buys nothing
            # and leaves a visible widget that has_captcha() then mistakes for a challenge.
            log.info("warm-up skipped (default): letting the site raise its own captcha")
        if args.check:
            st = page_state(driver)
            log.info("[--check] no login attempted. state=%s", st)
            log.info("[--check] captcha sdk: %s", captcha_sdk_ready(driver))
            # selenium can only see cookies for the current document's domain, so hop to the
            # captcha host to check whether tdc.js planted its copy over there at all.
            for probe in ("https://turing.captcha.qcloud.com/", "https://t.captcha.qq.com/"):
                try:
                    driver.get(probe)
                    time.sleep(1.5)
                    log.info("[--check] %s cookies: %s", probe, sorted(cookie_names(driver)))
                except Exception as exc:
                    log.info("[--check] %s probe failed: %s", probe, type(exc).__name__)
            return 0

        if args.manual:
            driver.get(LOGIN_URL)                      # clear the stale RK001 banner
            time.sleep(6)
            qr = driver.execute_script(
                "const t=document.querySelector('.qr_code'); if(t){t.click(); return true;} return false;")
            log.info("[--manual] window ready, qr tab selected=%s; waiting for you to scan", qr)
            print(">>> Chrome 已就绪，" + ("已切到扫码页签，请用手机扫码" if qr
                 else "请手动切到「手机扫码」页签")
                  + f"；最多等 {args.wait_minutes} 分钟。", flush=True)
            if not wait_for_human_login(driver, args.wait_minutes):
                log.error("no login detected within %d minutes", args.wait_minutes)
                return 3
            outcome = "loggedIn"
        else:
            if args.intercept_f06:
                enable_f06_block(driver)
                log.info("f06 blocker armed (the login POST will not leave the browser)")
            log.info("form: %s", fill_and_submit(driver, args.account, password))
            if args.intercept_f06:
                time.sleep(3)
                intercepted = read_blocked_f06(driver)
                log.info("blocked %d f06 request(s) client-side", len(intercepted))
                if args.har:
                    diff_against_har(intercepted, args.har)
                else:
                    log.warning("no --har given; dumping our request only")
                    for rec in intercepted[-1:]:
                        print(json.dumps(rec, ensure_ascii=False, indent=1))
                return 0
            outcome = wait_for_challenge(driver)
            log.info("outcome: %s", outcome)

            if outcome == "captcha":
                handler = make_handler(driver)
                if args.captcha == "agent":
                    ok = agent_solve_captcha(driver, handler, timeout=args.agent_timeout)
                elif args.captcha == "llm":
                    ok = llm_solve_captcha(driver, handler)
                elif args.captcha == "off":
                    ok = False
                    log.info("--captcha off: leaving the challenge for a human")
                else:
                    ok = handler.solve_point_click_captcha(driver)
                log.info("captcha (%s) solved=%s", args.captcha, ok)
                outcome = wait_for_challenge(driver, 25)
                log.info("after captcha: %s", outcome)

        if not (page_state(driver)["loggedIn"] or not page_state(driver)["on_login"]):
            log.error("login did not complete (%s); not harvesting data", outcome)
            dump_login_state(driver, tag=f"after:{outcome}",
                             shot=Path("trace") / f"{time.strftime('%H%M%S')}-{outcome.split(':')[0]}.png")
            return 3

        if sess:
            save_session(driver, sess)      # capture first: a crash mid-walk must not cost the scan
        walked = harvest_routes(driver, reload=True,
                                routes_out=Path(args.json_out + ".routes"),
                                only=picked_only)
        records = [r for v in walked.values() for r in v]
        if not records:
            records = browse_and_collect(driver)
            walked = {"(fallback scrape)": records}
        Path(args.json_out).write_text(
            json.dumps(walked, ensure_ascii=False, indent=1), encoding="utf-8")
        save_api_inventory(driver, Path(args.json_out + ".api"))
        export_browser_auth(driver, Path("sgcc_auth.json"))
        if args.by_meter:
            collected = harvest_by_meter(
                driver, [r.strip() for r in args.by_meter.split(",") if r.strip()],
                Path(args.json_out + ".meters.json"))
            if args.push:
                push_harvest(collected)
        log.info("harvested %d decrypted API payloads across %d routes -> %s",
                 len(records), len(walked), args.json_out)
        for rec in records[:14]:
            log.info("  %-42s keys=%s", str(rec.get("url") or "?")[-40:], (rec.get("keys") or [])[:6])
        if sess:
            save_session(driver, sess)
        if args.watch_ttl:
            watch_ttl(driver, minutes=args.ttl_interval_min, max_hours=args.ttl_max_hours,
                      csv_path=Path("ttl_watch.csv"))
        return 0
    finally:
        driver.quit()


if __name__ == "__main__":
    sys.exit(main())
