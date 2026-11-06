"""Browser automation scenario recorded to video (Playwright, sync API).

Public API:
    PATH_TO_VIDEO_FOLDER  -- where the .webm files are saved
    CaptureVideo_Magento  -- run the sample scenario for one CSV row
    ShowCallout           -- show a yellow text callout on the current page
    LAST_RUN              -- details of the last CaptureVideo_Magento call
"""

import logging
import math
import os
import random
import re
import threading
import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright

PATH_TO_VIDEO_FOLDER = "./video"

# Extra knobs (may be overridden by the main script, like PATH_TO_VIDEO_FOLDER).
# DuckDuckGo answers headless Chromium with "Unexpected error", so headed is the default.
HEADLESS = False
VIDEO_SIZE = {"width": 1280, "height": 720}
RETRIES = 1                 # extra attempts per row after a failure
ATTEMPT_TIMEOUT_S = 120     # hard budget for one attempt (browser start to video saved)
RESULTS_TIMEOUT_MS = 30000  # how long to wait for search results (also after a reload)
# Measured on DuckDuckGo: results normally render 5-10 s after the search, but about
# 1 in 4 results pages stays blank for good; a reload then shows them within seconds.
RESULTS_RELOAD_AFTER_MS = 15000

SEARCH_BOX = 'textarea[name="q"]'
SEARCH_BUTTON = 'form button[type="submit"]'
FIRST_RESULT = 'a[data-testid="result-title-a"]'

# Details of the last CaptureVideo_Magento call (status, error, clicked, final_url, ...).
LAST_RUN = {}

log = logging.getLogger("qscript")

_local = threading.local()  # .session: the scenario ShowCallout() draws on
_produced = set()           # video paths already claimed in this process

_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {f"{p}{i}" for p in ("COM", "LPT")
                                                  for i in range(1, 10)}
_MAX_NAME_BYTES = 150  # leaves room for "_FAILED", "_2" and ".webm" under the 255 limit

# Injected into every document of the context (survives full navigations).
# Plain IIFE: add_init_script evaluates the string as-is. Styles are set through
# the CSSOM and animations through the Web Animations API (no <style>, no
# innerHTML), so strict CSP / Trusted Types sites still render the overlay.
_OVERLAY_SCRIPT = r"""
(() => {
  if (window.__qc) return;
  const CURSOR = 32;
  // Same start point as the Python side, so the cursor is visible from frame one.
  const state = { x: innerWidth * 0.75, y: innerHeight * 0.8,
                  root: null, cursor: null, halo: null };
  window.__qc = state;

  try {
    const saved = JSON.parse(sessionStorage.getItem('__qcPos') || 'null');
    if (saved) { state.x = saved.x; state.y = saved.y; }
  } catch (e) {}

  const css = (el, props) => { Object.assign(el.style, props); return el; };
  const div = (id, props) => {
    const d = document.createElement('div');
    if (id) d.id = id;
    return css(d, props);
  };
  const NO_HIT = { pointerEvents: 'none', position: 'fixed', left: '0', top: '0' };

  function arrow() {
    const NS = 'http://www.w3.org/2000/svg';
    const svg = document.createElementNS(NS, 'svg');
    svg.setAttribute('width', CURSOR);
    svg.setAttribute('height', CURSOR);
    svg.setAttribute('viewBox', '0 0 32 32');
    const path = document.createElementNS(NS, 'path');
    path.setAttribute('d', 'M2 1 L2 26 L8.5 20 L13 30.5 L17.5 28.5 L13 18.5 L22 18.5 Z');
    path.setAttribute('fill', '#ffffff');
    path.setAttribute('stroke', '#000000');
    path.setAttribute('stroke-width', '2');
    path.setAttribute('stroke-linejoin', 'round');
    svg.appendChild(path);
    return svg;
  }

  function place() {
    if (!state.cursor) return;
    state.cursor.style.transform = `translate(${state.x}px, ${state.y}px)`;
    state.halo.style.transform = `translate(${state.x}px, ${state.y}px)`;
  }

  function mount() {
    if (document.getElementById('__qc-root') || !document.documentElement) return;
    const root = div('__qc-root', { ...NO_HIT, right: '0', bottom: '0', zIndex: '2147483647' });
    const halo = div('__qc-halo', { ...NO_HIT, width: `${CURSOR * 2}px`, height: `${CURSOR * 2}px`,
      margin: `-${CURSOR}px 0 0 -${CURSOR}px`, borderRadius: '50%',
      background: 'rgba(255, 221, 0, 0.45)' });
    const cursor = div('__qc-cursor', { ...NO_HIT, width: `${CURSOR}px`, height: `${CURSOR}px`,
      filter: 'drop-shadow(1px 2px 2px rgba(0,0,0,.35))' });
    cursor.appendChild(arrow());
    root.append(halo, cursor);   // halo first -> behind the arrow
    document.documentElement.appendChild(root);
    Object.assign(state, { root, cursor, halo });
    place();
  }

  window.__qcSet = (x, y) => {
    state.x = x; state.y = y; place();
    try { sessionStorage.setItem('__qcPos', JSON.stringify({ x, y })); } catch (e) {}
  };

  window.__qcRipple = (x, y) => {
    mount();
    const r = div(null, { ...NO_HIT, left: x + 'px', top: y + 'px',
      width: `${CURSOR * 2}px`, height: `${CURSOR * 2}px`,
      margin: `-${CURSOR}px 0 0 -${CURSOR}px`, borderRadius: '50%', boxSizing: 'border-box',
      border: '4px solid rgb(255, 110, 0)', background: 'rgba(255, 110, 0, 0.18)' });
    // Above the yellow highlight (same size, so it would hide the wave), below the arrow.
    state.root.insertBefore(r, state.cursor);
    // Stays bright while it grows, fades out at full size.
    r.animate([{ transform: 'scale(0.1)', opacity: 1 },
               { transform: 'scale(0.75)', opacity: 1, offset: 0.55 },
               { transform: 'scale(1)', opacity: 0 }],
              { duration: 700, easing: 'ease-out', fill: 'forwards' });
    setTimeout(() => r.remove(), 800);
  };

  function box(text, bg, fg, fadeMs) {
    mount();
    const old = document.getElementById('__qc-callout');
    if (old) old.remove();
    const b = div('__qc-callout', { ...NO_HIT, left: '50%', top: 'auto', bottom: '48px',
      transform: 'translateX(-50%)', maxWidth: '80vw', width: 'max-content',
      padding: '12px 20px', borderRadius: '8px', whiteSpace: 'pre-wrap',
      font: '600 16pt/1.35 -apple-system, "Segoe UI", Arial, sans-serif',
      boxShadow: '0 4px 16px rgba(0,0,0,.25)', background: bg, color: fg,
      opacity: '0', transition: `opacity ${fadeMs}ms ease` });
    b.textContent = text;
    state.root.appendChild(b);
    requestAnimationFrame(() => requestAnimationFrame(() => { b.style.opacity = '1'; }));
    return b;
  }

  window.__qcCallout = (text, bg, fg, fadeMs, holdMs) => new Promise((resolve) => {
    const b = box(text, bg, fg, fadeMs);
    setTimeout(() => {
      b.style.opacity = '0';
      setTimeout(() => { b.remove(); resolve(true); }, fadeMs + 50);
    }, fadeMs + holdMs);
  });

  // Persistent, non-blocking status line ("Searching..."); null hides it.
  window.__qcStatus = (text) => {
    const cur = document.getElementById('__qc-callout');
    if (text === null) { if (cur && cur.dataset.status) cur.remove(); return; }
    if (cur && cur.dataset.status === text) return;
    box(text, '#FFDD00', '#000000', 300).dataset.status = text;
  };

  window.addEventListener('mousemove', (e) => window.__qcSet(e.clientX, e.clientY), true);
  window.addEventListener('mousedown', (e) => window.__qcRipple(e.clientX, e.clientY), true);

  mount();
  document.addEventListener('DOMContentLoaded', mount);
  // Some SPAs rebuild <html> children; re-attach the overlay if it gets dropped.
  setInterval(mount, 500);
})();
"""


class AttemptTimeout(Exception):
    """The attempt ran out of its ATTEMPT_TIMEOUT_S budget."""


class _Session:
    """State of one recording attempt: the page, the mouse position, the deadline."""

    def __init__(self, page, timeout_s):
        self.page = page
        self.deadline = time.monotonic() + timeout_s
        self.x = VIDEO_SIZE["width"] * 0.75
        self.y = VIDEO_SIZE["height"] * 0.8

    def check(self):
        if time.monotonic() > self.deadline:
            raise AttemptTimeout(f"attempt exceeded {ATTEMPT_TIMEOUT_S}s")

    def budget_ms(self, wanted_ms):
        """Cap a wait so it never runs past the attempt deadline."""
        self.check()
        left = (self.deadline - time.monotonic()) * 1000
        return max(500, min(wanted_ms, left))


def ShowCallout(text, bgColor="#FFDD00", textColor="#000000", fade=True,
                page=None, hold_seconds=1.5):
    """Show a rectangle with text (auto-sized), optionally fading in/out ~2 s each.

    Blocks until the callout has disappeared. Draws on `page`, or on the page
    of the scenario currently being recorded in this thread.
    """
    session = getattr(_local, "session", None)
    page = page or (session.page if session else None)
    if page is None:
        raise RuntimeError("ShowCallout: no active page")
    fade_ms = 2000 if fade else 0
    try:
        page.evaluate(
            "([t, bg, fg, f, h]) => window.__qcCallout(t, bg, fg, f, h)",
            [text, bgColor, textColor, fade_ms, int(hold_seconds * 1000)],
        )
    except PlaywrightError as e:
        # A callout is decoration only; never fail the scenario because of it.
        log.warning("callout skipped: %s", e.message.splitlines()[0])


def _status(s, text):
    try:
        s.page.evaluate("t => window.__qcStatus && window.__qcStatus(t)", text)
    except PlaywrightError:
        pass  # document is being replaced; the next poll will retry


def _pause(s, low, high):
    s.page.wait_for_timeout(s.budget_ms(random.uniform(low, high) * 1000))


def _sync_overlay(s):
    """Push the Python-side mouse position into a freshly loaded document."""
    try:
        s.page.evaluate("([x, y]) => window.__qcSet && window.__qcSet(x, y)", [s.x, s.y])
    except PlaywrightError:
        pass


def _move_mouse(s, x, y):
    """Glide the mouse to (x, y) along a slightly curved, eased path."""
    s.check()
    sx, sy = s.x, s.y
    dist = math.hypot(x - sx, y - sy)
    steps = max(15, min(80, int(dist / 12)))
    # Control point offset perpendicular to the path -> gentle arc.
    bend = random.uniform(-0.15, 0.15) * dist
    nx, ny = (-(y - sy) / dist, (x - sx) / dist) if dist else (0, 0)
    cx, cy = (sx + x) / 2 + nx * bend, (sy + y) / 2 + ny * bend
    for i in range(1, steps + 1):
        t = i / steps
        t = 3 * t * t - 2 * t * t * t  # ease-in-out
        px = (1 - t) ** 2 * sx + 2 * (1 - t) * t * cx + t * t * x
        py = (1 - t) ** 2 * sy + 2 * (1 - t) * t * cy + t * t * y
        s.page.mouse.move(px, py)
        s.page.wait_for_timeout(12)
    s.x, s.y = x, y


def _aim(box):
    # A little left of centre for long links, like a person would.
    x = box["x"] + min(box["width"] / 2, 40 + random.uniform(0, 30))
    y = box["y"] + box["height"] / 2 + random.uniform(-2, 2)
    return x, y


def _inside(box, x, y, pad=2):
    return (box["x"] + pad <= x <= box["x"] + box["width"] - pad
            and box["y"] + pad <= y <= box["y"] + box["height"] - pad)


def _wait_stable(s, loc, settle_ms=800, timeout_ms=8000):
    """Wait until the element stops moving (results keep re-flowing while loading)."""
    deadline = time.monotonic() + s.budget_ms(timeout_ms) / 1000
    last, since = None, time.monotonic()
    while time.monotonic() < deadline:
        box = loc.bounding_box()
        key = box and (round(box["x"]), round(box["y"]))
        if key != last:
            last, since = key, time.monotonic()
        elif time.monotonic() - since >= settle_ms / 1000:
            return
        s.page.wait_for_timeout(100)


def _move_to_element(s, selector, timeout_ms=None):
    """Glide onto the first visible match; re-aim if the layout shifted meanwhile."""
    timeout_ms = RESULTS_TIMEOUT_MS if timeout_ms is None else timeout_ms
    # DuckDuckGo keeps hidden duplicates (e.g. a 0-width submit in the header).
    loc = s.page.locator(f"{selector} >> visible=true").first
    loc.wait_for(state="visible", timeout=s.budget_ms(timeout_ms))
    loc.scroll_into_view_if_needed(timeout=s.budget_ms(5000))
    _wait_stable(s, loc)
    for _ in range(4):
        box = loc.bounding_box()
        if not box:
            raise RuntimeError(f"element has no box: {selector}")
        if _inside(box, s.x, s.y):
            return loc
        _move_mouse(s, *_aim(box))
    raise RuntimeError(f"element keeps moving, cannot aim at: {selector}")


def _hit(loc, x, y):
    """True if the topmost element at (x, y) is `loc` or inside it."""
    return loc.evaluate(
        "(el, [x, y]) => { const t = document.elementFromPoint(x, y);"
        " return !!t && (t === el || el.contains(t)); }",
        [x, y],
    )


# What is under (x, y) instead of `el`. A floating element (cookie banner, promo
# popup) is hidden; anything else means the layout moved and we must re-aim.
_UNCOVER_JS = """(el, [x, y]) => {
  const t = document.elementFromPoint(x, y);
  if (!t || el.contains(t)) return null;
  const name = (n) => (n.tagName + (n.id ? '#' + n.id : '') +
                       (typeof n.className === 'string' && n.className ? '.' + n.className : ''))
                      .slice(0, 80);
  for (let n = t; n && n !== document.body && n !== document.documentElement; n = n.parentElement) {
    const pos = getComputedStyle(n).position;
    if (pos === 'fixed' || pos === 'sticky') {
      n.style.setProperty('visibility', 'hidden', 'important');
      return { hidden: name(n) };
    }
  }
  return { under: name(t) };
}"""


def _ensure_clickable(s, loc, selector):
    """Make sure `loc` is the topmost element under the cursor before clicking."""
    info = None
    for _ in range(3):
        if _hit(loc, s.x, s.y):
            return loc
        s.page.keyboard.press("Escape")  # closes many popups the polite way
        s.page.wait_for_timeout(300)
        if _hit(loc, s.x, s.y):
            return loc
        info = loc.evaluate(_UNCOVER_JS, [s.x, s.y])
        if info and "hidden" in info:
            log.warning("hid an element covering the target: %s", info["hidden"])
        elif info:
            log.info("cursor is over %s, not the target; re-aiming", info["under"])
        loc = _move_to_element(s, selector)
    raise RuntimeError(f"{selector} stays covered (last seen under cursor: {info})")


def _click_here(s):
    """Real mouse click at the current position (triggers default actions)."""
    s.page.mouse.down()
    s.page.wait_for_timeout(random.uniform(60, 120))
    s.page.mouse.up()


def _type_human(s, text):
    for ch in text:
        s.check()
        s.page.keyboard.type(ch)
        delay = random.uniform(0.06, 0.18)
        if ch == " ":
            delay += random.uniform(0.05, 0.2)
        s.page.wait_for_timeout(delay * 1000)


def _wait_for_results(s, start_url):
    """Wait for the first result across the full-page navigation of the search.

    Shows a "Searching..." status meanwhile and reloads a blank results page once.
    """
    started = time.monotonic()
    reloaded = False
    while True:
        s.check()
        if s.page.url != start_url:
            _status(s, "Searching…")
        try:
            s.page.wait_for_selector(f"{FIRST_RESULT} >> visible=true", timeout=1500)
            _status(s, None)
            return
        except PlaywrightTimeoutError:
            pass
        except PlaywrightError as e:
            msg = e.message.lower()
            if "context was destroyed" not in msg and "navigation" not in msg:
                raise
        elapsed_ms = (time.monotonic() - started) * 1000
        if not reloaded and elapsed_ms > RESULTS_RELOAD_AFTER_MS:
            log.warning("no results after %ds, reloading the results page",
                        RESULTS_RELOAD_AFTER_MS // 1000)
            s.page.reload(wait_until="domcontentloaded", timeout=s.budget_ms(30000))
            _sync_overlay(s)
            reloaded = True
        elif elapsed_ms > RESULTS_RELOAD_AFTER_MS + RESULTS_TIMEOUT_MS:
            raise PlaywrightTimeoutError(f"no search results ({FIRST_RESULT})")


def _scenario(s, url, query, intro):
    """Steps 1-7 of the sample behavior. Returns details for LAST_RUN."""
    page = s.page
    # 1. Open the page; 2. the SVG cursor comes from the init script.
    page.goto(url, wait_until="domcontentloaded", timeout=s.budget_ms(45000))
    _sync_overlay(s)
    ShowCallout(intro, fade=True, hold_seconds=1)

    # 3. Glide to the search box and focus it.
    box = _move_to_element(s, SEARCH_BOX)
    _pause(s, 0.2, 0.4)
    _click_here(s)
    _pause(s, 0.3, 0.6)

    # 4. Type the query like a human.
    box.fill("")
    _type_human(s, query)
    _pause(s, 0.4, 0.8)

    # 5. Click the search button (full page navigation follows).
    start_url = page.url
    try:
        _move_to_element(s, SEARCH_BUTTON, timeout_ms=10000)
        _pause(s, 0.2, 0.4)
        _click_here(s)
    except PlaywrightTimeoutError:
        log.warning("search button not visible, pressing Enter")
        page.keyboard.press("Enter")
    except PlaywrightError as e:
        if "context was destroyed" not in e.message.lower():
            raise

    # 6. Wait for the client-side rendered results.
    _wait_for_results(s, start_url)
    _sync_overlay(s)
    _pause(s, 1.0, 1.5)

    # 7. Glide to the first result and click it (in this tab, so the video shows it).
    result = _move_to_element(s, FIRST_RESULT)
    _pause(s, 0.3, 0.6)
    result = _ensure_clickable(s, result, FIRST_RESULT)
    title = result.inner_text().strip()
    href = result.get_attribute("href")
    result.evaluate("el => el.removeAttribute('target')")
    log.info("clicking first result: %s", title[:80])
    results_url = page.url
    _click_here(s)
    try:
        page.wait_for_url(lambda u: u != results_url, timeout=s.budget_ms(20000))
        page.wait_for_load_state("domcontentloaded", timeout=s.budget_ms(20000))
    except PlaywrightTimeoutError:
        log.warning("result page is slow to load: %s", href)
    _sync_overlay(s)
    _pause(s, 2.5, 3.0)
    return {"clicked": title, "href": href, "final_url": page.url}


def _attempt(url, folder, query, intro, language):
    """One recording attempt. Returns (ok, raw_video_path, details_or_error)."""
    raw_video, details = None, {}
    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=HEADLESS)
        try:
            context = browser.new_context(
                viewport=VIDEO_SIZE,
                record_video_dir=folder,
                record_video_size=VIDEO_SIZE,
                locale=_locale(language),
                bypass_csp=True,
            )
            context.set_default_timeout(30000)
            context.add_init_script(_OVERLAY_SCRIPT)
            page = context.new_page()
            raw_video = page.video.path() if page.video else None
            s = _Session(page, ATTEMPT_TIMEOUT_S)
            _local.session = s
            try:
                details = _scenario(s, url, query, intro)
                ok = True
            except Exception as e:  # noqa: BLE001 - any failure ends this attempt
                ok = False
                details = {"error": f"{type(e).__name__}: {str(e).splitlines()[0]}"}
                log.debug("attempt failed", exc_info=True)
            finally:
                _local.session = None
                context.close()  # finalizes the .webm
        finally:
            browser.close()
    return ok, raw_video, details


def _safe_filename(name):
    """Turn a company name into a portable file name stem."""
    name = re.sub(r'[\\/:*?"<>|\x00-\x1f]+', "_", name or "").strip(" .")
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = "_" + name
    raw = name.encode("utf-8")
    if len(raw) > _MAX_NAME_BYTES:
        name = raw[:_MAX_NAME_BYTES].decode("utf-8", errors="ignore").rstrip(" .")
    return name or "video"


def _claim_target(folder, company_name):
    """Path for this row's video; duplicates within one run get _2, _3, ..."""
    stem = _safe_filename(company_name)
    path = os.path.join(folder, stem + ".webm")
    n = 2
    while path in _produced:
        path = os.path.join(folder, f"{stem}_{n}.webm")
        n += 1
    if n > 2:
        log.warning("duplicate company name %r, saving as %s", company_name,
                    os.path.basename(path))
    _produced.add(path)
    return path


def _locale(language):
    lang = (language or "").strip().replace("_", "-")
    return lang if re.fullmatch(r"[A-Za-z]{2,3}(-[A-Za-z]{2})?", lang) else None


def CaptureVideo_Magento(
    url,
    company_name,
    language,
    country,
    postcode,
    city,
    street,
    house_no,
):
    """Record the sample scenario for one CSV row.

    Returns the absolute path to <PATH_TO_VIDEO_FOLDER>/<company_name>.webm,
    or None on failure (the last partial video is kept as <name>_FAILED.webm).
    Details of the run are left in LAST_RUN.
    """
    started = time.monotonic()
    LAST_RUN.clear()
    folder = os.path.abspath(PATH_TO_VIDEO_FOLDER)
    os.makedirs(folder, exist_ok=True)
    target = _claim_target(folder, company_name)
    query = f"Magento developer in {city}"
    address = ", ".join(p for p in [f"{street} {house_no}".strip(),
                                    f"{postcode} {city}".strip(), country] if p)
    intro = f"{company_name}\n{address}" if address else company_name

    ok, raw_video, details = False, None, {}
    attempts = 0
    for attempts in range(1, RETRIES + 2):
        try:
            ok, raw_video, details = _attempt(url, folder, query, intro, language)
        except Exception as e:  # noqa: BLE001 - browser failed to start, etc.
            ok, details = False, {"error": f"{type(e).__name__}: {str(e).splitlines()[0]}"}
        if ok:
            break
        log.warning("attempt %d/%d failed: %s", attempts, RETRIES + 1, details.get("error"))
        if raw_video and os.path.exists(raw_video) and attempts <= RETRIES:
            os.remove(raw_video)  # only the last failed attempt is kept
        if attempts <= RETRIES:
            time.sleep(3)

    LAST_RUN.update(details, attempts=attempts, seconds=round(time.monotonic() - started, 1))
    if not raw_video or not os.path.exists(raw_video):
        LAST_RUN.update(status="failed", error=details.get("error") or "no video produced")
        return None
    if not ok:
        failed = target[: -len(".webm")] + "_FAILED.webm"
        os.replace(raw_video, failed)
        LAST_RUN.update(status="failed", video=failed)
        return None
    os.replace(raw_video, target)
    LAST_RUN.update(status="ok", video=target)
    return target
