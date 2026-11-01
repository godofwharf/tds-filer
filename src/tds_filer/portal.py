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

DEFAULT_MODEL = "github-copilot:gpt-4.1"


def _text(content) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    return "\n".join(getattr(p, "text", "") for p in content if getattr(p, "text", None))


def to_history(messages) -> tuple[list, str]:
    """browser-use messages -> (pydantic-ai message history, last user prompt)."""
    from pydantic_ai.messages import ModelRequest, ModelResponse, SystemPromptPart, TextPart, UserPromptPart
    hist: list = []
    pending: list = []
    for msg in messages:
        text = _text(msg.content)
        if msg.role == "system":
            pending.append(SystemPromptPart(content=text))
        elif msg.role == "user":
            pending.append(UserPromptPart(content=text))
        else:
            if pending:
                hist.append(ModelRequest(parts=pending))
                pending = []
            hist.append(ModelResponse(parts=[TextPart(content=text or "(no text)")]))
    if not pending or not isinstance(pending[-1], UserPromptPart):
        raise TdsError("last message to the model must be a user message")
    prompt = pending.pop().content
    if pending:
        hist.append(ModelRequest(parts=pending))
    return hist, prompt


TOKEN_FILE = Path("~/.config/tds-filer/copilot_token").expanduser()


def _copilot_token(token_file: Path | None = None, ask=None) -> str:
    """Env var, else the token stored on first use, else ask the user once and store it (0600)."""
    for var in ("GITHUB_COPILOT_API_KEY", "GITHUB_COPILOT_API_TOKEN", "COPILOT_GITHUB_TOKEN"):
        if os.environ.get(var):
            return os.environ[var]
    f = token_file or TOKEN_FILE
    if f.exists():
        if f.stat().st_mode & 0o077:
            f.chmod(0o600)
        tok = f.read_text().strip()
        if tok:
            return tok
    if ask is None:
        import getpass
        ask = getpass.getpass
    print("A GitHub Copilot OAuth token is needed (e.g. `gho_…` from a Copilot login). "
          f"It will be stored in {f} (mode 0600) and reused.")
    tok = ask("Copilot token (input hidden): ").strip()
    if not tok:
        raise TdsError("no Copilot token given")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.parent.chmod(0o700)
    fd = os.open(f, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as fh:
        fh.write(tok + "\n")
    return tok


def forget_copilot_token(token_file: Path | None = None) -> bool:
    f = token_file or TOKEN_FILE
    if f.exists():
        f.unlink()
        return True
    return False


def make_model(spec: str):
    """pydantic-ai model from 'provider:model' (e.g. github-copilot:claude-sonnet-5, anthropic:..., openai:...)."""
    if spec.startswith("github-copilot:"):
        from pydantic_ai.models.github_copilot import GitHubCopilotModel
        from pydantic_ai.providers.github_copilot import GitHubCopilotProvider
        return GitHubCopilotModel(spec.split(":", 1)[1], provider=GitHubCopilotProvider(api_key=_copilot_token()))
    from pydantic_ai.models import infer_model
    return infer_model(spec)


class PydanticAIChat:
    """browser-use BaseChatModel backed by pydantic-ai, so any pydantic-ai provider can drive the browser."""

    _verified_api_keys = True

    def __init__(self, spec: str, model=None):
        self.model = spec
        self._model = model if model is not None else make_model(spec)

    @property
    def provider(self) -> str:
        return self.model.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.model

    @property
    def model_name(self) -> str:
        return self.model

    async def ainvoke(self, messages, output_format=None, **kwargs):
        from browser_use.llm.exceptions import ModelProviderError
        from browser_use.llm.views import ChatInvokeCompletion
        from pydantic_ai import Agent
        hist, prompt = to_history(messages)
        agent = Agent(self._model, output_type=output_format or str)
        try:
            res = await agent.run(prompt, message_history=hist or None)
        except Exception as e:  # noqa: BLE001 - surface to browser-use's retry logic
            raise ModelProviderError(str(e), model=self.model) from e
        return ChatInvokeCompletion(completion=res.output, usage=None)


def profile_dir(cfg: Config) -> Path:
    d = cfg.filing_root / ".browser-profile"
    d.mkdir(parents=True, exist_ok=True)
    d.chmod(0o700)
    return d


def fetch(cfg: Config, qt: Quarter, qdir: Path, what: list[str], model: str,
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
        llm=PydanticAIChat(model),
        tools=tools,
        browser=browser,
        use_vision=False,
        sensitive_data={"x_tan": cfg.deductor["tan"]},
    )
    asyncio.run(agent.run(max_steps=60))
    return result
