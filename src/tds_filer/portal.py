"""Optional browser automation (browser-use) to fetch challans, CSI and the previous RRR from incometax.gov.in.

The agent never submits, uploads, pays or e-verifies anything; login is always done by the human.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from .config import Config, Quarter, TdsError

PORTAL = "https://eportal.incometax.gov.in/iec/foservices/#/login"
ALLOWED_DOMAINS = ["*.incometax.gov.in"]
WHAT = ("challans", "csi", "rrr")
RRR_RE = re.compile(r"\d{15}")

RULES = """Rules (mandatory):
- Stay on incometax.gov.in. Never click Submit, Upload, Pay, Proceed to payment, E-verify, or anything that files or pays.
- Only navigate, search, view and download.
- When you reach the login page, call wait_for_manual_login and do not type credentials yourself.
"""


@dataclass
class FetchResult:
    rrr: dict[str, str] = field(default_factory=dict)


def validate_rrr(rrr: str) -> str:
    rrr = rrr.strip()
    if not RRR_RE.fullmatch(rrr):
        raise TdsError(f"portal returned invalid RRR/token {rrr!r} (need 15 digits)")
    return rrr


def build_task(cfg: Config, qt: Quarter, what: list[str]) -> str:
    prev = qt.previous()
    steps = [f"Open {PORTAL}. Call wait_for_manual_login and wait until the user is logged in as TAN x_tan."]
    if "challans" in what:
        steps.append(
            f"Go to e-File -> e-Pay Tax -> Payment History. For every challan paid between {qt.start:%d-%b-%Y} and "
            f"{qt.end:%d-%b-%Y} (deposit date), download the challan receipt PDF.")
    if "csi" in what:
        steps.append(
            f"Download the Challan Status Inquiry (CSI) file for TAN x_tan for the date range "
            f"{qt.start:%d-%b-%Y} to {qt.end:%d-%b-%Y} (e-File -> e-Pay Tax / Services -> download CSI file).")
    if "rrr" in what:
        steps.append(
            f"Go to e-File -> Income Tax Forms -> View Filed Forms. Find Form 144 (or 27Q) for tax year {prev.fy} "
            f"quarter Q{prev.q}. Download its acknowledgement PDF and call report_rrr with its 15-digit token/receipt number.")
    steps.append("Finish with done, summarising which files you downloaded.")
    return RULES + "\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))


def _llm(provider: str, model: str | None):
    import browser_use as bu
    if provider == "anthropic":
        return bu.ChatAnthropic(model=model or "claude-sonnet-4-5")
    if provider == "openai":
        return bu.ChatOpenAI(model=model or "gpt-4.1")
    raise TdsError(f"unknown LLM provider {provider!r}")


def profile_dir(cfg: Config) -> Path:
    d = cfg.filing_root / ".browser-profile"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def fetch(cfg: Config, qt: Quarter, qdir: Path, what: list[str], provider: str, model: str | None,
          input_fn=input) -> FetchResult:
    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    try:
        import asyncio
        import browser_use as bu
    except ImportError as e:
        raise TdsError("portal extra not installed: uv tool install 'tds-filer[portal]' "
                       "(then: uvx browser-use install  # for Chromium)") from e

    qdir.mkdir(parents=True, exist_ok=True)
    qdir.chmod(0o700)
    result = FetchResult()
    tools = bu.Tools()

    @tools.action("Pause until the human has logged in manually (password/OTP/captcha).",
                  allowed_domains=ALLOWED_DOMAINS)
    async def wait_for_manual_login() -> bu.ActionResult:
        await asyncio.to_thread(input_fn, "\n>> Log in to the portal in the browser window, then press Enter here... ")
        return bu.ActionResult(extracted_content="User reports login complete.")

    @tools.action("Report the 15-digit RRR/token of a previously filed statement.", allowed_domains=ALLOWED_DOMAINS)
    async def report_rrr(rrr: str) -> bu.ActionResult:
        try:
            result.rrr[qt.previous().key] = validate_rrr(rrr)
        except TdsError as e:
            return bu.ActionResult(error=str(e))
        return bu.ActionResult(extracted_content="RRR recorded.")

    browser = bu.Browser(
        user_data_dir=str(profile_dir(cfg)),
        allowed_domains=ALLOWED_DOMAINS,
        downloads_path=str(qdir),
        accept_downloads=True,
        auto_download_pdfs=True,
        keep_alive=False,
        headless=False,
    )
    agent = bu.Agent(
        task=build_task(cfg, qt, what),
        llm=_llm(provider, model),
        tools=tools,
        browser=browser,
        use_vision=False,
        sensitive_data={"x_tan": cfg.deductor["tan"]},
    )
    asyncio.run(agent.run(max_steps=60))
    return result
