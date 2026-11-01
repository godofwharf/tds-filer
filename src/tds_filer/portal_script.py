"""Scripted (deterministic) portal workflow for `tds fetch --mode script`.

Each step is plain code driving the browser through browser-use's Actor API, using selectors recorded from the real
portal (see `tds fetch --mode record`). A step's result is verified by a check; if the scripted step fails, a guarded
browser-use agent is started for that step only (LLM fallback), and the check is run again.

Safety: every scripted click/fill passes portal.check_action() (same deny list as the agent guard), and agents use
portal.guarded_tools(). The script itself never uploads, submits, pays or e-verifies.
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

from . import portal
from .config import Config, Quarter, TdsError

LOGIN_URL = portal.PORTAL
LOGIN_URL_OVERRIDE = ""  # tests only
LOGIN_TIMEOUT = 600
BROWSER_OVERRIDES: dict = {}  # tests only (local fixture domain, headless)

MARK_JS = """(sel, rx, scope, mark) => {
  const root = scope ? document.querySelector(scope) : document;
  if (!root) return JSON.stringify([]);
  const re = rx ? new RegExp(rx, 'i') : null;
  const out = [];
  for (const el of root.querySelectorAll(sel)) {
    const r = el.getBoundingClientRect();
    if (r.width === 0 && r.height === 0) continue;
    const t = (el.innerText || el.value || el.getAttribute('aria-label') || '').replace(/\\s+/g, ' ').trim();
    if (re && !re.test(t)) continue;
    el.setAttribute('data-tds', mark + '-' + out.length);
    out.push(t.slice(0, 600));
  }
  return JSON.stringify(out);
}"""

JS_CLICK = """(m) => {
  const e = document.querySelector('[data-tds="' + m + '"]');
  if (!e) return false;
  e.scrollIntoView({block: 'center'});
  for (const t of ['pointerenter', 'mouseenter', 'mouseover', 'mousemove'])
    e.dispatchEvent(new MouseEvent(t, {bubbles: t !== 'mouseenter' && t !== 'pointerenter'}));
  e.click();
  return true;
}"""

# Selectors recorded from the portal (Nov 2026).
SEL = {
    "efile": "a#e-File",
    "menuitem": "[role=menuitem]",
    "dashboard": "[role=link]",
    "form_card": "mat-card.eachMatCardStyle",
    "view_all": "span.hyperLink",
    "sub_card": "mat-card.subCard",
    "button": "button",
    "rrr_dialog": "#rrrNumDownloadModel",
    "rrr_dialog_buttons": "#rrrNumDownloadModel .modal-footer button",
    "act_radio": "mat-radio-button",
    "tab": "[role=tab]",
    "grid_rows": "div.ag-center-cols-container [role=row]",
    "row_menu": "button[aria-label^='Example icon-button']",
    "pager": "button.buttonPag",
    "csi_from": "input#fromcsipayment",
    "csi_to": "input#topayment",
}
FORM144_CARD_RE = r"deduction of tax.*other than salary|Form\s*144|27Q"
DATE_RE = re.compile(r"\b(\d{2}-[A-Z][a-z]{2}-\d{4})\b")
CIN_RE = re.compile(r"\b(\d{14}[A-Z]{4})\b")


class StepError(Exception):
    pass


# --- pure helpers (unit-tested) -------------------------------------------------------------------------------
def parse_rrr(dialog_text: str) -> str:
    m = re.search(r"RRR\s*Number\D*(\d{15})\b", dialog_text) or re.search(r"\b(\d{15})\b", dialog_text)
    if not m:
        raise StepError("no 15-digit RRR in the receipt dialog")
    return portal.validate_rrr(m.group(1))


def card_regex(qt: Quarter) -> str:
    return rf"T\.?\s*Y\.?\s*{re.escape(qt.fy)}\s*\(\s*Q{qt.q}\s*\)"


def parse_row(text: str) -> tuple[str, date] | None:
    """(CIN, payment date) from an ag-Grid Payment History row's text."""
    c, d = CIN_RE.search(text), DATE_RE.search(text)
    if not (c and d):
        return None
    return c.group(1), datetime.strptime(d.group(1), "%d-%b-%Y").date()


def plan_page(rows: list[str], qt: Quarter) -> tuple[list[tuple[int, str, date]], bool]:
    """Rows to download on this page (index, CIN, date) and whether to stop paging (a row older than the quarter)."""
    todo, stop = [], False
    for i, t in enumerate(rows):
        p = parse_row(t)
        if not p:
            continue
        cin, d = p
        if qt.start <= d <= qt.end:
            todo.append((i, cin, d))
        elif d < qt.start:
            stop = True
    return todo, stop


def act_label(qt: Quarter) -> str:
    return r"Income-tax Act,\s*2025" if qt.fy_start >= 2026 else r"Income-tax Act,\s*1961"


# --- browser driver -------------------------------------------------------------------------------------------
class Driver:
    def __init__(self, page, qdir: Path, audit: Path, blocked: list[str]):
        self.page, self.qdir, self.audit, self.blocked = page, qdir, audit, blocked
        self._n = 0

    def _log(self, rec: dict) -> None:
        fd = os.open(self.audit, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps({"ts": time.time(), "source": "script", **rec}, default=str) + "\n")

    async def url(self) -> str:
        return await self.page.get_url()

    async def find(self, sel: str, text: str | None = None, scope: str | None = None) -> tuple[str, list[str]]:
        self._n += 1
        mark = f"m{self._n}"
        raw = await self.page.evaluate(MARK_JS, sel, text or "", scope or "", mark)
        return mark, json.loads(raw) if raw else []

    async def wait_for(self, sel: str, text: str | None = None, scope: str | None = None,
                       timeout: float = 30) -> tuple[str, list[str]]:
        end = time.monotonic() + timeout
        while True:
            mark, found = await self.find(sel, text, scope)
            if found:
                return mark, found
            if time.monotonic() > end:
                raise StepError(f"timed out waiting for {sel} {text or ''}".strip())
            await asyncio.sleep(0.5)

    async def _element(self, mark: str, nth: int):
        els = await self.page.get_elements_by_css_selector(f'[data-tds="{mark}-{nth}"]')
        if not els:
            raise StepError(f"element {mark}-{nth} vanished")
        return els[0]

    async def _guard(self, action: str, label: str) -> None:
        url = await self.url()
        reason = portal.check_action(action, {"index": 0}, label, url)
        self._log({"action": action, "url": url, "label": label[:300], "blocked": reason})
        if reason:
            self.blocked.append(f"script {action}: {reason}")
            raise StepError(f"safety guard blocked {action}: {reason}")

    async def click(self, sel: str, text: str | None = None, scope: str | None = None, nth: int = 0,
                    timeout: float = 30, how: str = "actor") -> str:
        mark, found = await self.wait_for(sel, text, scope, timeout)
        if nth >= len(found):
            raise StepError(f"only {len(found)} match(es) for {sel} {text or ''}")
        await self._guard("click", f"{sel} {found[nth]}")
        if how == "js":
            if not await self.page.evaluate(JS_CLICK, f"{mark}-{nth}"):
                raise StepError(f"element {mark}-{nth} vanished")
        else:
            try:
                await (await self._element(mark, nth)).click()
            except StepError:
                raise
            except Exception as e:  # noqa: BLE001 - CDP node detached mid-click (Angular re-render)
                self._log({"action": "click_retry_js", "label": f"{sel} {found[nth]}", "error": str(e)[:200]})
                mark, found2 = await self.wait_for(sel, text, scope, timeout=5)
                if nth >= len(found2) or not await self.page.evaluate(JS_CLICK, f"{mark}-{nth}"):
                    raise StepError(f"click failed ({e}) and element is gone") from e
        return found[nth]

    async def fill(self, sel: str, value: str, timeout: float = 30) -> None:
        mark, found = await self.wait_for(sel, None, None, timeout)
        await self._guard("input", f"{sel} {found[0]}")
        el = await self._element(mark, 0)
        await el.fill(value)
        await self.page.press("Tab")

    async def wait_download(self, pattern: str, since: float, timeout: float = 40) -> Path | None:
        rx = re.compile(pattern, re.I)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            for p in self.qdir.iterdir():
                if p.is_file() and rx.search(p.name) and p.stat().st_mtime >= since - 1 \
                        and not p.name.endswith((".crdownload", ".part")):
                    s1 = p.stat().st_size
                    await asyncio.sleep(0.5)
                    if p.stat().st_size == s1 and s1 > 0:
                        return p
            await asyncio.sleep(0.5)
        return None


# --- steps ----------------------------------------------------------------------------------------------------
@dataclass
class StepResult:
    name: str
    ok: bool
    via: str = "script"
    seconds: float = 0
    error: str = ""
    detail: dict = field(default_factory=dict)


async def login(d: Driver, input_fn=print) -> None:
    await d.page.goto(LOGIN_URL_OVERRIDE or LOGIN_URL)
    end = time.monotonic() + 15
    while time.monotonic() < end:
        if "#/dashboard" in await d.url():
            return
        await asyncio.sleep(1)
    input_fn("\n>> Log in to the portal in the browser window (password/OTP/captcha). Waiting for the dashboard...")
    end = time.monotonic() + LOGIN_TIMEOUT
    while time.monotonic() < end:
        if "#/dashboard" in await d.url():
            await d.wait_for(SEL["efile"], timeout=60)
            return
        await asyncio.sleep(2)
    raise StepError("login not completed in time")


async def go_dashboard(d: Driver) -> None:
    if (await d.url()).rstrip("/").endswith("#/dashboard"):
        await d.wait_for(SEL["efile"], timeout=30)
        return
    await d.click(SEL["dashboard"], r"^Dashboard$")
    await d.wait_for(SEL["efile"], timeout=60)
    await asyncio.sleep(1)


def menu_regex(label: str) -> str:
    return rf"^{re.escape(label)}(\s|$)"


async def open_menu(d: Driver, *labels: str) -> None:
    """Angular Material submenus sometimes ignore a synthetic click; retry with a JS hover+click."""
    err = None
    for how in ("actor", "js", "actor"):
        try:
            await d.click(SEL["efile"], how=how)
            for label in labels:
                await d.click(SEL["menuitem"], menu_regex(label), timeout=8, how=how)
            return
        except Exception as e:  # noqa: BLE001 - any click/CDP failure -> next strategy
            err = e if isinstance(e, StepError) else StepError(f"{type(e).__name__}: {e}")
            _, items = await d.find(SEL["menuitem"])
            d._log({"action": "menu_retry", "how": how, "error": str(e), "visible_menuitems": items[:20]})
            await d.page.press("Escape")
            await asyncio.sleep(1)
    raise StepError(f"{err}; visible menu items: {items[:20]}")


async def step_rrr(d: Driver, qt: Quarter, ctx: dict) -> dict:
    prev = qt.previous()
    for attempt in (1, 2):
        await go_dashboard(d)
        await open_menu(d, "Income Tax Forms", "View Filed Forms")
        try:
            mark, _ = await d.wait_for(SEL["form_card"], FORM144_CARD_RE, timeout=40)
            break
        except StepError:
            if attempt == 2:
                raise
    await d.click(SEL["view_all"], r"^View All$", scope=f'[data-tds="{mark}-0"]')
    mark, cards = await d.wait_for(SEL["sub_card"], card_regex(prev), timeout=40)
    await d.click(SEL["button"], r"^Download Receipt$", scope=f'[data-tds="{mark}-0"]')
    _, dlg = await d.wait_for(SEL["rrr_dialog"], r"RRR", timeout=20)
    rrr = parse_rrr(dlg[0])
    ctx["rrr"][prev.key] = rrr
    since = time.time()
    ack = None
    try:
        await d.click(SEL["rrr_dialog_buttons"], r"^Download$", timeout=5)
        ack = await d.wait_download(r"\.pdf$", since, timeout=20)
    except StepError:
        pass
    try:
        await d.click(SEL["rrr_dialog_buttons"], r"^Cancel$", timeout=3)
    except StepError:
        pass
    return {"rrr": rrr, "card": cards[0][:80], "ack": ack.name if ack else None}


async def open_epay(d: Driver, qt: Quarter) -> None:
    await go_dashboard(d)
    await open_menu(d, "e-Pay Tax")
    end = time.monotonic() + 40
    while time.monotonic() < end:
        _, radios = await d.find(SEL["act_radio"], act_label(qt))
        if radios:
            await d.click(SEL["act_radio"], act_label(qt))
            await d.click(SEL["button"], r"^Continue$")
            break
        _, tabs = await d.find(SEL["tab"], r"^Payment History$")
        if tabs:
            break
        await asyncio.sleep(0.5)
    await d.wait_for(SEL["tab"], r"^Payment History$", timeout=40)


async def step_challans(d: Driver, qt: Quarter, ctx: dict) -> dict:
    await open_epay(d, qt)
    await d.click(SEL["tab"], r"^Payment History$")
    got, have = [], []
    first_prev = None
    for _page in range(10):
        mark, rows = await d.wait_for(SEL["grid_rows"], timeout=40)
        if rows[0] == first_prev:
            await asyncio.sleep(1)
            mark, rows = await d.find(SEL["grid_rows"])
        todo, stop = plan_page(rows, qt)
        for i, cin, _dt in todo:
            existing = list(d.qdir.glob(f"{cin}*ChallanReceipt*.pdf"))
            if existing:
                have.append(existing[0].name)
                continue
            since = time.time()
            await d.click(SEL["row_menu"], None, scope=f'[data-tds="{mark}-{i}"]')
            await d.click(SEL["menuitem"], r"^Download$", timeout=10)
            f = await d.wait_download(rf"{cin}.*\.pdf$|ChallanReceipt.*\.pdf$", since, timeout=40)
            if not f:
                raise StepError(f"receipt for {cin} did not download")
            got.append(f.name)
            mark, rows = await d.find(SEL["grid_rows"])
        if stop:
            break
        first_prev = rows[0] if rows else None
        pmark, pagers = await d.find(SEL["pager"])
        if len(pagers) < 3:
            break
        await d.click(SEL["pager"], None, nth=2)
        await asyncio.sleep(1.5)
    return {"downloaded": got, "already_present": have}


async def step_csi(d: Driver, qt: Quarter, ctx: dict) -> dict:
    if "e-pay-tax" not in await d.url():
        await open_epay(d, qt)
    await d.click(SEL["tab"], r"^Challan Status Inquiry \(CSI\) File$")
    await d.fill(SEL["csi_from"], f"{qt.start:%d-%b-%Y}")
    await d.fill(SEL["csi_to"], f"{qt.end:%d-%b-%Y}")
    since = time.time()
    await d.click(SEL["button"], r"^Download Challan File$")
    f = await d.wait_download(r"\.csi$", since, timeout=40)
    if not f:
        raise StepError("CSI file did not download")
    return {"csi": f.name}


# --- checks (independent of how the step ran) -----------------------------------------------------------------
def check_rrr(qt: Quarter, qdir: Path, ctx: dict, since: float) -> bool:
    return bool(ctx["rrr"].get(qt.previous().key))


def check_challans(qt: Quarter, qdir: Path, ctx: dict, since: float) -> bool:
    from . import challan
    months = set()
    for p in qdir.glob("*ChallanReceipt*.pdf"):
        try:
            c = challan.parse_challan(challan.pdf_text(p), p.name)
        except Exception:  # noqa: BLE001
            continue
        if qt.contains(c.dt):
            months.add((c.dt.year, c.dt.month))
    return months == set(qt.months)


def check_csi(qt: Quarter, qdir: Path, ctx: dict, since: float) -> bool:
    tan = ctx["tan"]
    return any(p.name.upper().startswith(tan) and p.stat().st_mtime >= since - 1 for p in qdir.glob("*.csi"))


STEPS = {
    "rrr": (step_rrr, check_rrr),
    "challans": (step_challans, check_challans),
    "csi": (step_csi, check_csi),
}
ORDER = ("rrr", "challans", "csi")


def fallback_task(cfg: Config, qt: Quarter, name: str) -> str:
    full = portal.build_task(cfg, qt, [name])
    rules, _, steps = full.partition("\n1. ")
    items = re.split(r"\n\d+\. ", "\n1. " + steps)[1:]
    body = [s for s in items if not s.startswith(("Open ", "Finish with done"))]
    return (rules + "\nYou are already logged in; the browser is on the portal. Do only this:\n"
            + "\n".join(f"{i}. {s}" for i, s in enumerate(body, 1)) + "\nThen finish with done.")


async def run(cfg: Config, qt: Quarter, qdir: Path, what: list[str], llm, log_dir: Path,
              input_fn=print) -> tuple[dict, list[StepResult], list[str]]:
    """llm=None disables the per-step agent fallback."""
    import browser_use as bu
    blocked: list[str] = []
    ctx = {"rrr": {}, "tan": cfg.deductor["tan"].upper()}
    results: list[StepResult] = []
    kw = dict(user_data_dir=str(portal.profile_dir(cfg)), allowed_domains=portal.ALLOWED_DOMAINS,
              downloads_path=str(qdir), accept_downloads=True, auto_download_pdfs=True, keep_alive=True,
              headless=False)
    kw.update(BROWSER_OVERRIDES)
    browser = bu.Browser(**kw)
    await browser.start()
    try:
        page = await browser.get_current_page() or await browser.new_page()
        d = Driver(page, qdir, log_dir / "guard.jsonl", blocked)
        await login(d, input_fn)
        for name in [n for n in ORDER if n in what]:
            fn, check = STEPS[name]
            t0, since = time.monotonic(), time.time()
            r = StepResult(name, False)
            try:
                r.detail = await fn(d, qt, ctx)
            except Exception as e:  # noqa: BLE001 - any failure -> fallback
                r.error = f"{type(e).__name__}: {e}"[:300]
            r.ok = check(qt, qdir, ctx, since)
            if not r.ok and llm is not None and len(blocked) < portal.MAX_BLOCKS:
                r.via = "agent"
                await _agent_step(bu, cfg, qt, name, ctx, browser, llm, blocked, log_dir)
                r.ok = check(qt, qdir, ctx, since)
            r.seconds = round(time.monotonic() - t0, 1)
            results.append(r)
            _append(log_dir / "script_steps.jsonl", r.__dict__)
    finally:
        await browser.kill()
    return ctx, results, blocked


async def _agent_step(bu, cfg, qt, name, ctx, browser, llm, blocked, log_dir) -> None:
    tools = portal.guarded_tools(bu, blocked, audit=log_dir / "guard.jsonl")
    prev_key = qt.previous().key

    @tools.action("Report the 15-digit RRR/token of a previously filed statement.",
                  allowed_domains=portal.ALLOWED_DOMAINS)
    async def report_rrr(rrr: str) -> bu.ActionResult:
        try:
            ctx["rrr"][prev_key] = portal.validate_rrr(rrr)
        except TdsError as e:
            return bu.ActionResult(error=str(e))
        return bu.ActionResult(extracted_content="RRR recorded.")

    agent = bu.Agent(task=fallback_task(cfg, qt, name), llm=llm, tools=tools, browser=browser, use_vision=False,
                     use_judge=False, directly_open_url=False, sensitive_data={"x_tan": cfg.deductor["tan"]},
                     save_conversation_path=str(log_dir / f"conversation-{name}"))
    hist = await agent.run(max_steps=15)
    out = log_dir / f"agent-{name}"
    out.mkdir(mode=0o700, exist_ok=True)
    portal.save_run_log(hist, out)


def _append(p: Path, rec: dict) -> None:
    fd = os.open(p, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    with os.fdopen(fd, "a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")


def fetch(cfg: Config, qt: Quarter, qdir: Path, what: list[str], model: str, fallback: bool = True,
          input_fn=print) -> "portal.FetchResult":
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    try:
        import browser_use  # noqa: F401
    except ImportError as e:
        raise TdsError("portal extra not installed: uv tool install 'tds-filer[portal]'") from e
    qdir.mkdir(parents=True, exist_ok=True)
    qdir.chmod(0o700)
    result = portal.FetchResult()
    result.log_dir = log_dir = portal.new_log_dir(cfg, qt)
    llm = portal.PydanticAIChat(model, transcript=log_dir / "llm_calls.jsonl") if fallback else None
    print(f"  scripted run (LLM fallback: {'on, ' + model if fallback else 'off'}); logging to {log_dir}")
    t_start = time.time()
    ctx, steps, blocked = asyncio.run(run(cfg, qt, qdir, what, llm, log_dir, input_fn))
    result.rrr, result.blocked, result.steps = ctx["rrr"], blocked, steps
    result.new_files = sorted(p.name for p in qdir.iterdir() if p.is_file() and p.stat().st_mtime >= t_start - 1)
    return result
