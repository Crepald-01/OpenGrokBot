"""The shared computer's browser: Playwright with one persistent profile (shared logins).

All Playwright objects live on a dedicated asyncio loop thread; callers use BrowserHost.call().
Every Bot gets its own Screen (a tab) in the shared context, so Bots run in parallel while
sharing cookies, sessions and app logins.
"""
from __future__ import annotations

import asyncio
import concurrent.futures
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable

from . import paths
from .events import EventBus
from .netpolicy import NetworkPolicy
from .settings import Settings


class BrowserError(RuntimeError):
    pass


SNAPSHOT_JS = r"""
() => {
  const MAX = 150;
  document.querySelectorAll('[data-gb-id]').forEach(e => e.removeAttribute('data-gb-id'));
  const sel = 'a[href],button,input,select,textarea,summary,[role=button],[role=link],[role=tab],[role=menuitem],[role=checkbox],[role=radio],[role=switch],[role=combobox],[role=option],[onclick],[contenteditable=true],[contenteditable=""],[tabindex]:not([tabindex="-1"])';
  const out = []; let n = 0; const vh = innerHeight, vw = innerWidth;
  for (const el of document.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width < 2 || r.height < 2) continue;
    const st = getComputedStyle(el);
    if (st.visibility === 'hidden' || st.display === 'none' || parseFloat(st.opacity) === 0 || el.disabled) continue;
    if (!(r.bottom > 0 && r.top < vh && r.right > 0 && r.left < vw)) continue;
    n++; el.setAttribute('data-gb-id', String(n));
    const tag = el.tagName.toLowerCase(); const type = (el.getAttribute('type') || '').toLowerCase();
    let label = (el.getAttribute('aria-label') || el.innerText || el.placeholder || el.title || el.alt || el.value || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    if (!label && el.labels && el.labels[0]) label = el.labels[0].innerText.trim().slice(0, 80);
    const isField = tag === 'input' || tag === 'textarea' || tag === 'select';
    out.push({id: n, tag, type, role: el.getAttribute('role') || '', label, href: tag === 'a' ? (el.getAttribute('href') || '').slice(0, 100) : '',
              value: isField && type !== 'password' ? String(el.value || '').slice(0, 60) : '', checked: el.checked === true,
              password: type === 'password', x: Math.round(r.left + r.width / 2), y: Math.round(r.top + r.height / 2)});
    if (n >= MAX) break;
  }
  return out;
}
"""

DESCRIBE_JS = r"""
(arg) => {
  let el = null;
  if (arg.id) el = document.querySelector('[data-gb-id="' + arg.id + '"]');
  else if (arg.x !== undefined) el = document.elementFromPoint(arg.x, arg.y);
  else el = document.activeElement;
  if (!el) return {found: false};
  const hit = el.closest('button,a,input,select,textarea,summary,[role=button],[role=link],label') || el;
  const form = hit.closest('form');
  const tag = hit.tagName.toLowerCase(); const type = (hit.getAttribute('type') || '').toLowerCase();
  return {found: true, tag, type, role: hit.getAttribute('role') || '',
          text: (hit.getAttribute('aria-label') || hit.innerText || hit.value || hit.title || '').trim().replace(/\s+/g, ' ').slice(0, 120),
          inForm: !!form, formAction: form ? (form.getAttribute('action') || location.href) : '',
          formHasPassword: form ? !!form.querySelector('input[type=password]') : false,
          explicitType: hit.hasAttribute('type'), href: hit.getAttribute('href') || '',
          autocomplete: (hit.getAttribute('autocomplete') || '').toLowerCase(), pageUrl: location.href};
}
"""

BLOCK_JS = r"""
() => {
  const t = ((document.title || '') + ' ' + (document.body ? document.body.innerText.slice(0, 3000) : '')).toLowerCase();
  const frames = Array.from(document.querySelectorAll('iframe')).map(f => (f.src || '').toLowerCase()).join(' ');
  const pw = !!Array.from(document.querySelectorAll('input[type=password]')).find(e => e.getBoundingClientRect().width > 0);
  return {t: t.slice(0, 3200), frames, pw};
}
"""

RECORDER_JS = r"""
(() => {
  if (window.__gbRec) return; window.__gbRec = true;
  const desc = (el) => {
    if (!el || !el.tagName) return {};
    const tag = el.tagName.toLowerCase(); const type = (el.getAttribute('type') || '').toLowerCase();
    const label = (el.getAttribute('aria-label') || (el.labels && el.labels[0] && el.labels[0].innerText) || el.innerText || el.placeholder || el.title || el.alt || '').trim().replace(/\s+/g, ' ').slice(0, 80);
    let selector = '';
    if (el.id) selector = '#' + el.id; else if (el.getAttribute('name')) selector = tag + '[name="' + el.getAttribute('name') + '"]';
    else if (el.getAttribute('data-testid')) selector = '[data-testid="' + el.getAttribute('data-testid') + '"]';
    else selector = tag + ((typeof el.className === 'string' && el.className.trim()) ? '.' + el.className.trim().split(/\s+/).slice(0, 2).join('.') : '');
    return {tag, type, label, selector, href: (el.getAttribute('href') || '').slice(0, 120), name: el.getAttribute('name') || ''};
  };
  const send = (o) => { try { window.__gbRecord(Object.assign({pageUrl: location.href}, o)); } catch (e) {} };
  addEventListener('click', e => { const el = e.target.closest('a,button,input,select,textarea,[role],label,summary') || e.target; send(Object.assign({action: 'click'}, desc(el))); }, true);
  addEventListener('change', e => { const el = e.target; const d = desc(el);
    const secret = d.type === 'password' || /^cc-/.test(el.getAttribute('autocomplete') || '');
    send(Object.assign({action: d.tag === 'select' ? 'select' : (d.type === 'checkbox' || d.type === 'radio' ? 'toggle' : 'type'),
      value: secret ? '<secret: user typed it>' : String(el.value || '').slice(0, 200)}, d)); }, true);
  addEventListener('keydown', e => { if (['Enter', 'Escape', 'Tab'].includes(e.key)) send(Object.assign({action: 'press', key: e.key}, desc(e.target))); }, true);
  addEventListener('submit', e => send(Object.assign({action: 'submit'}, desc(e.target))), true);
})();
"""

BLOCK_MARKERS = [
    ("captcha", "a CAPTCHA"), ("verify you are human", "a human-verification check"), ("are you a robot", "a bot check"),
    ("unusual traffic", "a bot/unusual-traffic check"), ("just a moment", "a browser check page"),
    ("checking your browser", "a browser check page"), ("press & hold", "a human-verification check"),
    ("access denied", "an access-denied page"), ("verification code", "a verification-code prompt (2FA)"),
    ("two-factor", "a two-factor prompt (2FA)"), ("2-step verification", "a 2-step verification prompt"),
    ("enter the code", "a code prompt (2FA)"), ("session expired", "an expired session"),
    ("your session has expired", "an expired session"), ("too many requests", "rate limiting"),
]


class Screen:
    """One Bot's screen: its own tab in the shared browser."""

    def __init__(self, host: "BrowserHost", bot_id: str):
        self.host, self.bot_id = host, bot_id
        self.page: Any = None
        self.pages: list[Any] = []
        self.takeover = False
        self.takeover_reason = ""
        self.recording: dict | None = None
        self.last_dialog = ""
        self.last_status = 0
        self.busy = False
        self.last_shot: str = ""

    # -- page management ------------------------------------------------------
    async def ensure_page(self):
        ctx = await self.host._context()
        if self.page is None or self.page.is_closed():
            self.pages = [p for p in self.pages if not p.is_closed()]
            if self.pages:
                self.page = self.pages[-1]
            else:
                await self._adopt(await ctx.new_page())
        return self.page

    async def _adopt(self, page) -> None:
        self.page = page
        self.pages.append(page)
        self.host._page_owner[id(page)] = self.bot_id
        await page.route("**/*", self._route)
        page.on("popup", lambda p: asyncio.ensure_future(self._adopt(p)))
        page.on("dialog", lambda d: asyncio.ensure_future(self._dialog(d)))
        page.on("close", lambda *_: self._closed(page))
        page.on("response", self._response)
        page.on("download", lambda d: asyncio.ensure_future(self._download(d)))
        page.on("framenavigated", lambda f: self._navigated(page, f))

    def _closed(self, page) -> None:
        self.pages = [p for p in self.pages if p is not page]
        if self.page is page:
            self.page = self.pages[-1] if self.pages else None

    async def _route(self, route, request=None):
        req = route.request
        url = req.url
        if not url.startswith(("http://", "https://")):
            await route.continue_()
            return
        ok, why = self.host.net.check(self.bot_id, url)
        if ok:
            await route.continue_()
        else:
            if req.resource_type == "document":
                self.last_dialog = f"Navigation blocked by network policy: {why}"
            await route.abort("blockedbyclient")

    def _response(self, resp) -> None:
        try:
            if resp.request.resource_type == "document" and resp.frame == resp.frame.page.main_frame:
                self.last_status = resp.status
        except Exception:
            pass

    async def _dialog(self, d) -> None:
        self.last_dialog = f"A {d.type} dialog appeared: \"{d.message[:200]}\" (it was {'accepted' if d.type in ('alert', 'beforeunload') else 'dismissed'})"
        try:
            if d.type in ("alert", "beforeunload"):
                await d.accept()
            else:
                await d.dismiss()
        except Exception:
            pass

    async def _download(self, d) -> None:
        dest_dir = paths.workspace_dir() / "downloads"
        name = re.sub(r"[^\w.\- ]", "_", d.suggested_filename or "download.bin")
        dest = dest_dir / name
        n = 1
        while dest.exists():
            dest = dest_dir / f"{dest.stem}-{n}{dest.suffix}"
            n += 1
        try:
            await d.save_as(str(dest))
            self.host._emit("download", bot_id=self.bot_id, path=str(dest), url=d.url)
            self.last_dialog = f"A file was downloaded to {dest}"
        except Exception as e:  # noqa: BLE001
            self.last_dialog = f"Download failed: {e}"

    def _navigated(self, page, frame) -> None:
        if self.recording is not None and frame == page.main_frame and not self.takeover_input_pending():
            self._record({"action": "navigate", "url": frame.url})

    def takeover_input_pending(self) -> bool:
        return False

    def _record(self, step: dict) -> None:
        rec = self.recording
        if rec is None or rec.get("paused"):
            return
        steps = rec["steps"]
        if steps and steps[-1].get("action") == step.get("action") == "navigate" and steps[-1].get("url") == step.get("url"):
            return
        if steps and step.get("action") == "type" and steps[-1].get("action") == "type" and steps[-1].get("selector") == step.get("selector"):
            steps[-1] = {**step, "ts": time.time()}
            self.host._emit("recording_step", bot_id=self.bot_id, index=len(steps) - 1, step=steps[-1])
            return
        steps.append({**step, "ts": time.time()})
        self.host._emit("recording_step", bot_id=self.bot_id, index=len(steps) - 1, step=steps[-1])

    # -- navigation and observation -----------------------------------------
    async def goto(self, url: str) -> dict:
        page = await self.ensure_page()
        if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
            url = "https://" + url
        ok, why = self.host.net.check(self.bot_id, url)
        if not ok:
            raise BrowserError(f"Blocked by network policy: {why}")
        self.last_dialog = ""
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:  # noqa: BLE001
            msg = str(e)
            if "ERR_BLOCKED_BY_CLIENT" in msg:
                raise BrowserError(self.last_dialog or "Blocked by network policy.") from e
            if "net::ERR_" in msg:
                raise BrowserError("Could not load the page: " + msg.split("\n")[0].replace("Page.goto: ", "")) from e
            if "Timeout" not in msg:
                raise BrowserError(msg.split("\n")[0]) from e
        await self._settle()
        return await self.state()

    async def back(self) -> dict:
        page = await self.ensure_page()
        try:
            await page.go_back(wait_until="domcontentloaded", timeout=15000)
        except Exception:
            pass
        await self._settle()
        return await self.state()

    async def reload(self) -> dict:
        page = await self.ensure_page()
        await page.reload(wait_until="domcontentloaded", timeout=30000)
        await self._settle()
        return await self.state()

    async def _settle(self, extra: float = 0.4) -> None:
        page = self.page
        if page is None:
            return
        try:
            await page.wait_for_load_state("domcontentloaded", timeout=4000)
            await page.wait_for_load_state("networkidle", timeout=2500)
        except Exception:
            pass
        await asyncio.sleep(extra)

    async def state(self) -> dict:
        page = await self.ensure_page()
        try:
            title = await page.title()
        except Exception:
            title = ""
        return {"url": page.url, "title": title, "status": self.last_status, "tabs": len([p for p in self.pages if not p.is_closed()]),
                "notice": self.last_dialog}

    async def detect_block(self) -> list[str]:
        page = await self.ensure_page()
        reasons: list[str] = []
        try:
            info = await page.evaluate(BLOCK_JS)
        except Exception:
            return reasons
        t = info["t"]
        for marker, name in BLOCK_MARKERS:
            if marker in t and name not in reasons:
                reasons.append(name)
        if any(k in info["frames"] for k in ("recaptcha", "hcaptcha", "turnstile", "challenges.cloudflare.com", "arkoselabs")):
            reasons.append("a CAPTCHA widget")
        if info["pw"]:
            reasons.append("a login form (needs the user to sign in)")
        if self.last_status in (403, 429):
            reasons.append(f"HTTP {self.last_status} (the site is refusing automated access)")
        return list(dict.fromkeys(reasons))

    async def snapshot(self, text_chars: int = 5000) -> dict:
        page = await self.ensure_page()
        elements = await page.evaluate(SNAPSHOT_JS)
        try:
            text = await page.evaluate("() => (document.body ? document.body.innerText : '')")
        except Exception:
            text = ""
        text = re.sub(r"\n{3,}", "\n\n", text or "").strip()
        st = await self.state()
        return {**st, "elements": elements, "text": text[:text_chars], "text_truncated": len(text) > text_chars,
                "blocks": await self.detect_block()}

    async def read_text(self, selector: str | None = None, max_chars: int = 15000) -> dict:
        page = await self.ensure_page()
        try:
            text = await page.inner_text(selector or "body", timeout=5000)
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Could not read text: {str(e).splitlines()[0]}") from e
        text = re.sub(r"\n{3,}", "\n\n", text).strip()
        st = await self.state()
        return {**st, "text": text[:max_chars], "truncated": len(text) > max_chars}

    async def screenshot(self, full_page: bool = False, quality: int = 70, save: bool = True) -> tuple[bytes, str]:
        page = await self.ensure_page()
        try:
            data = await page.screenshot(type="jpeg", quality=quality, full_page=full_page, timeout=15000)
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Screenshot failed: {str(e).splitlines()[0]}") from e
        path = ""
        if save:
            p = paths.screenshots_dir() / f"shot-{self.bot_id[:6]}-{int(time.time() * 1000)}.jpg"
            p.write_bytes(data)
            path = str(p)
            self.last_shot = path
        return data, path

    # -- actions ---------------------------------------------------------------
    async def describe(self, *, element: int | None = None, x: int | None = None, y: int | None = None) -> dict:
        page = await self.ensure_page()
        arg: dict[str, Any] = {}
        if element:
            arg["id"] = element
        elif x is not None and y is not None:
            arg.update(x=x, y=y)
        try:
            return await page.evaluate(DESCRIBE_JS, arg)
        except Exception:
            return {"found": False}

    async def click(self, *, element: int | None = None, x: int | None = None, y: int | None = None,
                    double: bool = False, right: bool = False) -> dict:
        page = await self.ensure_page()
        before = page.url
        try:
            if element:
                loc = page.locator(f'[data-gb-id="{int(element)}"]').first
                await loc.click(timeout=8000, click_count=2 if double else 1, button="right" if right else "left")
            elif x is not None and y is not None:
                await page.mouse.click(x, y, click_count=2 if double else 1, button="right" if right else "left")
            else:
                raise BrowserError("Provide element (from browser_snapshot) or x and y.")
        except BrowserError:
            raise
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Click failed: {str(e).splitlines()[0]}. Take a fresh browser_snapshot; the page may have changed.") from e
        await self._settle()
        st = await self.state()
        st["navigated"] = st["url"] != before
        return st

    async def type_text(self, text: str, *, element: int | None = None, clear: bool = True, submit: bool = False) -> dict:
        page = await self.ensure_page()
        info = await self.describe(element=element)
        if info.get("found") and (info.get("type") == "password" or info.get("autocomplete", "").startswith(("current-password", "new-password", "cc-"))):
            raise BrowserError("This is a password/payment field. Bots never type secrets; call request_takeover so the user can enter it.")
        try:
            if element:
                loc = page.locator(f'[data-gb-id="{int(element)}"]').first
                await loc.click(timeout=6000)
                if clear:
                    await page.keyboard.press("Control+A")
                    await page.keyboard.press("Delete")
            await page.keyboard.type(text, delay=8)
            if submit:
                await page.keyboard.press("Enter")
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Typing failed: {str(e).splitlines()[0]}") from e
        await self._settle(0.3)
        return await self.state()

    async def select_option(self, element: int, value: str) -> dict:
        page = await self.ensure_page()
        loc = page.locator(f'[data-gb-id="{int(element)}"]').first
        try:
            try:
                await loc.select_option(label=value, timeout=5000)
            except Exception:
                await loc.select_option(value=value, timeout=5000)
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Select failed: {str(e).splitlines()[0]}") from e
        return await self.state()

    async def press(self, key: str) -> dict:
        page = await self.ensure_page()
        before = page.url
        try:
            await page.keyboard.press(key)
        except Exception as e:  # noqa: BLE001
            raise BrowserError(f"Key press failed: {str(e).splitlines()[0]}") from e
        await self._settle(0.4)
        st = await self.state()
        st["navigated"] = st["url"] != before
        return st

    async def scroll(self, direction: str = "down", amount: int = 600) -> dict:
        page = await self.ensure_page()
        dy = amount if direction == "down" else -amount if direction == "up" else 0
        dx = amount if direction == "right" else -amount if direction == "left" else 0
        await page.mouse.move(400, 300)
        await page.mouse.wheel(dx, dy)
        await asyncio.sleep(0.3)
        return await self.state()

    async def wait_for(self, text: str | None, seconds: float) -> dict:
        page = await self.ensure_page()
        if text:
            try:
                await page.get_by_text(text).first.wait_for(timeout=int(seconds * 1000))
            except Exception:
                pass
        else:
            await asyncio.sleep(min(seconds, 30))
        return await self.state()

    # -- takeover remote-desktop input -----------------------------------------
    async def user_input(self, ev: dict) -> dict:
        page = await self.ensure_page()
        t = ev.get("type")
        if t == "click":
            await page.mouse.click(float(ev["x"]), float(ev["y"]), button=ev.get("button", "left"), click_count=int(ev.get("count", 1)))
        elif t == "scroll":
            await page.mouse.move(float(ev.get("x", 400)), float(ev.get("y", 300)))
            await page.mouse.wheel(float(ev.get("dx", 0)), float(ev.get("dy", 0)))
        elif t == "key":
            await page.keyboard.press(str(ev["key"]))
        elif t == "text":
            await page.keyboard.type(str(ev["text"]), delay=5)
        elif t == "navigate":
            url = str(ev["url"])
            if not re.match(r"^[a-z][a-z0-9+.-]*://", url, re.I):
                url = "https://" + url
            try:
                await page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
        elif t == "back":
            await page.go_back(timeout=10000)
        elif t == "reload":
            await page.reload(timeout=20000)
        else:
            raise BrowserError(f"Unknown input type {t!r}")
        await asyncio.sleep(0.15)
        return await self.state()

    async def close(self) -> None:
        for p in list(self.pages):
            try:
                await p.close()
            except Exception:
                pass
        self.pages, self.page = [], None


class BrowserHost:
    def __init__(self, settings: Settings, net: NetworkPolicy, events: EventBus | None = None):
        self.settings, self.net, self.events = settings, net, events
        self.loop = asyncio.new_event_loop()
        self.thread = threading.Thread(target=self._run_loop, name="browser-host", daemon=True)
        self.thread.start()
        self._pw: Any = None
        self._ctx: Any = None
        self._ctx_lock: asyncio.Lock | None = None
        self.screens: dict[str, Screen] = {}
        self._page_owner: dict[int, str] = {}
        self._screens_lock = threading.Lock()
        self.on_event: Callable[[str, dict], None] | None = None
        self.last_error = ""

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def _emit(self, type_: str, **data: Any) -> None:
        if self.on_event:
            self.on_event(type_, data)

    def call(self, coro_fn: Callable, *args: Any, timeout: float = 90.0, **kwargs: Any) -> Any:
        fut = asyncio.run_coroutine_threadsafe(coro_fn(*args, **kwargs), self.loop)
        try:
            return fut.result(timeout)
        except concurrent.futures.TimeoutError as e:
            fut.cancel()
            raise BrowserError("The browser operation timed out.") from e

    async def _context(self):
        if self._ctx_lock is None:
            self._ctx_lock = asyncio.Lock()
        async with self._ctx_lock:
            if self._ctx is not None:
                try:
                    _ = self._ctx.pages
                    if not getattr(self._ctx, "_gb_dead", False):
                        return self._ctx
                except Exception:
                    pass
                self._ctx = None
            try:
                from playwright.async_api import async_playwright
            except ImportError as e:  # pragma: no cover
                raise BrowserError("Playwright is not installed. Run: pip install playwright") from e
            if self._pw is None:
                self._pw = await async_playwright().start()
            comp = self.settings.get("computer", {})
            try:
                self._ctx = await self._pw.chromium.launch_persistent_context(
                    str(paths.browser_profile_dir()), headless=bool(comp.get("headless", True)),
                    viewport={"width": int(comp.get("viewport_w", 1280)), "height": int(comp.get("viewport_h", 800))},
                    accept_downloads=True, locale="en-US")
            except Exception as e:  # noqa: BLE001
                msg = str(e)
                if "Executable doesn't exist" in msg or "playwright install" in msg:
                    self.last_error = "The browser engine is not installed. Run:  python -m playwright install chromium"
                elif "ProcessSingleton" in msg or "user data directory is already in use" in msg:
                    self.last_error = "The shared browser profile is locked by another process. Close other OpenGrokBot services."
                else:
                    self.last_error = "Could not start the browser: " + msg.splitlines()[0]
                raise BrowserError(self.last_error) from e
            self.last_error = ""
            self._ctx.on("close", lambda *_: setattr(self._ctx, "_gb_dead", True) if self._ctx else None)
            await self._ctx.expose_binding("__gbRecord", self._on_record)
            await self._ctx.add_init_script(RECORDER_JS)
            return self._ctx

    async def _on_record(self, source, data) -> None:
        page = source.get("page") if isinstance(source, dict) else getattr(source, "page", None)
        bot_id = self._page_owner.get(id(page))
        scr = self.screens.get(bot_id or "")
        if scr and scr.recording is not None:
            scr._record({k: v for k, v in data.items() if v not in (None, "")})

    def screen(self, bot_id: str) -> Screen:
        with self._screens_lock:
            scr = self.screens.get(bot_id)
            if scr is None:
                scr = self.screens[bot_id] = Screen(self, bot_id)
            return scr

    def status(self) -> dict:
        out = {}
        for bid, s in self.screens.items():
            alive = s.page is not None and not s.page.is_closed()
            out[bid] = {"bot_id": bid, "open": alive, "url": s.page.url if alive else "", "takeover": s.takeover,
                        "takeover_reason": s.takeover_reason, "recording": s.recording is not None, "busy": s.busy}
        return out

    def drop_screen(self, bot_id: str) -> None:
        scr = self.screens.pop(bot_id, None)
        if scr:
            try:
                self.call(scr.close, timeout=10)
            except Exception:
                pass

    def restart(self) -> None:
        """Re-launch the shared browser (e.g. after toggling headless)."""
        async def _stop():
            if self._ctx is not None:
                try:
                    await self._ctx.close()
                except Exception:
                    pass
            self._ctx = None
            for s in self.screens.values():
                s.page, s.pages = None, []
        try:
            self.call(_stop, timeout=20)
        except Exception:
            pass

    def shutdown(self) -> None:
        async def _stop():
            try:
                if self._ctx is not None:
                    await self._ctx.close()
                if self._pw is not None:
                    await self._pw.stop()
            except Exception:
                pass
        try:
            self.call(_stop, timeout=15)
        except Exception:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
