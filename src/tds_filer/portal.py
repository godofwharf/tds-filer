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
- After login, move only by clicking menus/tabs/buttons. Never navigate by typing a URL: it triggers a logout popup.
- Portal pages load slowly. If a page/list is still loading after a click, use ONE wait of 10 seconds, then re-check.
  If it is still not rendered after two such waits, re-open it once via the menu.
- A download is confirmed only when the file appears in your available files list. Do not click the same download
  button more than twice; if no file appears, note it and move on.
"""


@dataclass
class FetchResult:
    rrr: dict[str, str] = field(default_factory=dict)
    log_dir: Path | None = None
    new_files: list[str] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


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
            f"Go to e-File -> e-Pay Tax (select the Act if asked, Continue) -> Payment History tab. Rows are sorted by "
            f"deposit date, newest first, and paginated. Download the challan receipt (row menu -> Download) for every "
            f"row with deposit date from {qt.start:%d-%b-%Y} to {qt.end:%d-%b-%Y}. Go to the next page until you see a "
            f"row dated before {qt.start:%d-%b-%Y} (then stop; older pages cannot contain in-range rows). "
            f"Expect about one challan per month ({len(qt.months)}).")
    if "csi" in what:
        steps.append(
            f"In e-Pay Tax, open the 'Challan Status Inquiry (CSI) File' tab, set From {qt.start:%d-%b-%Y} and To "
            f"{qt.end:%d-%b-%Y}, click 'Download Challan File', and confirm a .csi file appears.")
    if "rrr" in what:
        steps.append(
            f"Open e-File -> Income Tax Forms -> View Filed Forms. On the Form 144 (or 27Q) card click 'View All'. "
            f"Find the filing for T.Y. {prev.fy}, Quarter Q{prev.q} (check both on the card). Click its 'Download "
            f"Receipt': the dialog shows the 15-digit RRR/token. Immediately call report_rrr with it. Then click the "
            f"dialog's Download once to save the acknowledgement PDF; if no file appears after one 10 s wait, move on "
            f"(the RRR is what matters).")
    steps.append("Finish with done: list each downloaded file, the RRR reported, and anything you could not get.")
    return RULES + "\n" + "\n".join(f"{i}. {s}" for i, s in enumerate(steps, 1))

# --- Safety guard (enforced in code, not by the prompt) ---------------------------------------------------------
# The agent may only view and download. These actions can put a local file into a page or run arbitrary JS / leave
# the portal, so they are removed from the agent entirely.
EXCLUDED_ACTIONS = ["upload_file", "evaluate", "search"]
# Element labels the agent may never click/type into/select (upload, filing, e-verification, payment, signing).
DENY_TEXT = re.compile(
    r"upload|\bsubmit|e-?\s?verif|everif|file\s+income\s+tax\s+form|\bfile\s+(?:now|form|return)\b|"
    r"pay\s*now|proceed\s+to\s+pay|make\s+payment|payment\s+gateway|net\s*banking|new\s+payment|create\s+challan|"
    r"\bproceed\b|\battach|\bbrowse\b|choose\s+file|select\s+file|\bdsc\b|digital\s+signature|\bsign\b|"
    r"generate\s+evc|aadhaar\s+otp|declaration|\bi\s+agree\b",
    re.I)
# Portal pages the agent may never be on (filing/upload, e-verify, payment creation).
DENY_URL = re.compile(
    r"upload|e-?verif|everif|payment-?gateway|new-?payment|create-?challan|netbanking|"
    r"/pay(?:ment)?/(?:proceed|confirm)", re.I)
ELEMENT_ACTIONS = {"click", "input", "select_dropdown", "dropdown_options", "send_keys"}
SAFE_ON_DENIED_PAGE = {"go_back", "wait", "done", "close", "navigate"}
MAX_BLOCKS = 3


def element_label(node) -> str:
    """Text a human would associate with a DOM element (tag, key attributes, accessible name, inner text)."""
    if node is None:
        return ""
    attrs = getattr(node, "attributes", None) or {}
    parts = [getattr(node, "tag_name", "") or ""]
    parts += [f"{k}={attrs[k]}" for k in ("type", "id", "name", "aria-label", "title", "value", "formcontrolname",
                                          "href", "for", "accept") if attrs.get(k)]
    ax = getattr(node, "ax_node", None)
    if ax is not None and getattr(ax, "name", None):
        parts.append(ax.name)
    try:
        parts.append(node.get_all_children_text()[:200])
    except Exception:  # noqa: BLE001
        pass
    return " ".join(parts)


def check_action(name: str, params: dict | None, label: str = "", page_url: str = "",
                 is_file_input: bool = False) -> str | None:
    """Return a reason if the action must be blocked, else None. Fail closed."""
    params = params or {}
    if name in EXCLUDED_ACTIONS:
        return f"action '{name}' is disabled"
    if page_url and DENY_URL.search(page_url) and name not in SAFE_ON_DENIED_PAGE:
        return f"current page is a filing/upload/e-verify/payment page ({page_url.split('?')[0]})"
    if name == "navigate" and DENY_URL.search(str(params.get("url", ""))):
        return f"navigation to {params.get('url')} is blocked"
    if name in ELEMENT_ACTIONS and params.get("index") is not None:
        if is_file_input:
            return "file inputs are blocked"
        m = DENY_TEXT.search(label)
        if m:
            return f"element '{label[:80]}' matches blocked term '{m.group(0)}'"
    if name == "send_keys" and params.get("index") is None and page_url and DENY_URL.search(page_url):
        return "keys on a blocked page"
    return None


def guarded_tools(bu, blocked: list[str], audit: Path | None = None):
    """bu.Tools subclass that checks every action against check_action() before browser-use executes it."""

    class GuardedTools(bu.Tools):
        async def act(self, action, browser_session, *args, **kwargs):
            for name, params in action.model_dump(exclude_unset=True).items():
                if params is None:
                    continue
                params = params if isinstance(params, dict) else {}
                label, url, is_file = "", "", False
                try:
                    url = await browser_session.get_current_page_url()
                except Exception:  # noqa: BLE001
                    url = ""
                if name in ELEMENT_ACTIONS and params.get("index") is not None:
                    node = await browser_session.get_element_by_index(params["index"])
                    if node is None:
                        reason = f"element {params['index']} not found (cannot verify it is safe)"
                    else:
                        label = element_label(node)
                        attrs = getattr(node, "attributes", None) or {}
                        is_file = (getattr(node, "tag_name", "") == "input" and attrs.get("type", "").lower() == "file")
                        reason = check_action(name, params, label, url, is_file)
                else:
                    reason = check_action(name, params, label, url)
                if audit is not None:
                    import json
                    import time
                    fd = os.open(audit, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
                    with os.fdopen(fd, "a") as fh:
                        fh.write(json.dumps({"ts": time.time(), "action": name, "params": params, "url": url,
                                             "label": label[:300], "blocked": reason}, default=str) + "\n")
                if reason:
                    blocked.append(f"{name}: {reason}")
                    if len(blocked) >= MAX_BLOCKS:
                        return bu.ActionResult(is_done=True, success=False, extracted_content=(
                            f"Stopped by tds-filer safety guard after {len(blocked)} blocked actions."))
                    return bu.ActionResult(error=(
                        f"BLOCKED by tds-filer safety guard: {reason}. Uploading, filing, paying, signing and "
                        f"e-verifying are disabled in code; only view and download."))
            return await super().act(action, browser_session, *args, **kwargs)

    return GuardedTools(exclude_actions=list(EXCLUDED_ACTIONS))


OUTPUT_RETRIES = 3
DEFAULT_MODEL = "github-copilot:claude-sonnet-5.5"


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
    print("A GitHub Copilot token is needed: a fine-grained PAT (github_pat_…) with the Copilot Requests permission, or an OAuth token (gho_…). "
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


def copilot_integration_id(token: str) -> str | None:
    """Copilot rejects fine-grained PATs under pydantic-ai's default 'vscode-chat'; use the SDK id for them."""
    if os.environ.get("GITHUB_COPILOT_INTEGRATION_ID"):
        return os.environ["GITHUB_COPILOT_INTEGRATION_ID"]
    return "copilot-sdk" if token.startswith("github_pat_") else None


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
        tok = _copilot_token()
        iid = copilot_integration_id(tok)
        return GitHubCopilotModel(spec.split(":", 1)[1], provider=GitHubCopilotProvider(api_key=tok, integration_id=iid))
    from pydantic_ai.models import infer_model
    return infer_model(spec)


def _wrap_output(output_format, mode: str):
    if output_format is None:
        return str
    from pydantic_ai import NativeOutput, PromptedOutput, ToolOutput
    return {"tool": ToolOutput, "native": NativeOutput, "prompted": PromptedOutput}[mode](output_format)


class PydanticAIChat:
    """browser-use BaseChatModel backed by pydantic-ai, so any pydantic-ai provider can drive the browser."""

    _verified_api_keys = True

    def __init__(self, spec: str, model=None, transcript: Path | None = None, output_mode: str = "prompted"):
        self.output_mode = output_mode
        self.model = spec
        self._model = model if model is not None else make_model(spec)
        self.transcript = transcript

    @property
    def provider(self) -> str:
        return self.model.split(":", 1)[0]

    @property
    def name(self) -> str:
        return self.model

    @property
    def model_name(self) -> str:
        return self.model

    def _log(self, rec: dict) -> None:
        if not self.transcript:
            return
        import json
        fd = os.open(self.transcript, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
        with os.fdopen(fd, "a") as fh:
            fh.write(json.dumps(rec, default=str) + "\n")

    async def ainvoke(self, messages, output_format=None, **kwargs):
        import time
        from browser_use.llm.exceptions import ModelProviderError
        from browser_use.llm.views import ChatInvokeCompletion
        from pydantic_ai import Agent
        hist, prompt = to_history(messages)
        agent = Agent(self._model, output_type=_wrap_output(output_format, self.output_mode), retries=OUTPUT_RETRIES)
        rec = {"ts": time.time(), "model": self.model, "output_format": getattr(output_format, "__name__", None),
               "n_messages": len(messages), "input_chars": sum(len(_text(m.content)) for m in messages),
               "last_message": _text(messages[-1].content)}
        t0 = time.monotonic()
        try:
            res = await agent.run(prompt, message_history=hist or None)
        except Exception as e:  # noqa: BLE001 - surface to browser-use's retry logic
            self._log({**rec, "seconds": round(time.monotonic() - t0, 2), "error": str(e),
                       "cause": repr(e.__cause__)[:2000] if e.__cause__ else None})
            raise ModelProviderError(str(e), model=self.model) from e
        from pydantic_ai.messages import RetryPromptPart
        retries = [str(p.content)[:500] for m in res.new_messages() for p in getattr(m, "parts", [])
                   if isinstance(p, RetryPromptPart)]
        u = res.usage()
        out = res.output.model_dump() if hasattr(res.output, "model_dump") else res.output
        self._log({**rec, "seconds": round(time.monotonic() - t0, 2), "output": out, "retries": retries, "requests": getattr(u, "requests", None),
                   "input_tokens": getattr(u, "input_tokens", None), "output_tokens": getattr(u, "output_tokens", None)})
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
    log_dir = new_log_dir(cfg, qt)
    result.log_dir = log_dir
    tools = guarded_tools(bu, result.blocked, audit=log_dir / "guard.jsonl")

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

    llm = PydanticAIChat(model, transcript=log_dir / "llm_calls.jsonl")
    preflight(llm)
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
        llm=llm,
        tools=tools,
        browser=browser,
        use_vision=False,
        sensitive_data={"x_tan": cfg.deductor["tan"]},
        use_judge=False,
        save_conversation_path=str(log_dir / "conversation"),
        file_system_path=str(log_dir / "agent_fs"),
    )
    print(f"  logging this run to {log_dir}")
    before = {p.name for p in qdir.iterdir()}
    hist = None
    try:
        hist = asyncio.run(agent.run(max_steps=60))
    finally:
        if hist is None:
            hist = getattr(agent, "history", None)
        if hist is not None:
            save_run_log(hist, log_dir)
        result.new_files = sorted(p.name for p in qdir.iterdir() if p.name not in before)
    if not hist.is_done():
        errs = [e for e in hist.errors() if e]
        raise TdsError("portal agent did not finish" + (f": {errs[-1][:300]}" if errs else ""))
    return result


def new_log_dir(cfg: Config, qt: Quarter) -> Path:
    from datetime import datetime
    root = cfg.filing_root / ".fetch-logs"
    d = root / f"{qt.fy}-Q{qt.q}-{datetime.now():%Y%m%d-%H%M%S}"
    d.mkdir(parents=True, exist_ok=True)
    for p in (root, d):
        p.chmod(0o700)
    return d


def save_run_log(hist, log_dir: Path) -> None:
    """history.json (browser-use format), steps.md (readable summary); screenshots moved out of $TMPDIR."""
    import json
    import shutil
    shots = log_dir / "screenshots"
    for p in hist.screenshot_paths():
        if p and Path(p).exists():
            shots.mkdir(exist_ok=True)
            shutil.move(p, shots / Path(p).name)
            src_root = Path(p).parent.parent
            if src_root.name.startswith("browser_use_agent_"):
                shutil.rmtree(src_root, ignore_errors=True)
    hist.save_to_file(str(log_dir / "history.json"))
    data = json.loads((log_dir / "history.json").read_text())
    (log_dir / "steps.md").write_text(summarize_history(data))
    for p in log_dir.rglob("*"):
        p.chmod(0o700 if p.is_dir() else 0o600)


def summarize_history(data: dict) -> str:
    """Readable per-step table + aggregates from a browser-use history.json dict."""
    from collections import Counter
    steps = data.get("history", [])
    lines, actions, urls, errors = [], Counter(), Counter(), 0
    total = 0.0
    for i, h in enumerate(steps, 1):
        mo = h.get("model_output") or {}
        md = h.get("metadata") or {}
        st = h.get("state") or {}
        dur = (md.get("step_end_time") or 0) - (md.get("step_start_time") or 0)
        total += max(dur, 0)
        acts = []
        for a in mo.get("action") or []:
            for name, params in a.items():
                actions[name] += 1
                acts.append(f"{name}({', '.join(f'{k}={str(v)[:40]}' for k, v in (params or {}).items())})")
        url = (st.get("url") or "").split("?")[0]
        urls[url] += 1
        errs = [r.get("error") for r in h.get("result") or [] if r.get("error")]
        errors += bool(errs)
        lines.append(f"### Step {md.get('step_number', i)}  ({dur:.1f}s)  {url}\n"
                     f"- eval: {mo.get('evaluation_previous_goal', '')}\n"
                     f"- next: {mo.get('next_goal', '')}\n"
                     f"- actions: {'; '.join(acts) or '-'}\n"
                     + "".join(f"- ERROR: {e[:300]}\n" for e in errs))
    head = [f"# browser-use run: {len(steps)} steps, {total:.0f}s, {errors} step(s) with errors", "",
            "## Actions", *[f"- {n}: {c}" for n, c in actions.most_common()], "",
            "## Steps per URL", *[f"- {c}  {u}" for u, c in urls.most_common()], "", "## Steps", ""]
    return "\n".join(head + lines)


def preflight(llm) -> None:
    """One tiny model call before opening the browser, so auth/model problems fail fast."""
    import asyncio
    from browser_use.llm.messages import UserMessage
    try:
        asyncio.run(llm.ainvoke([UserMessage(content="Reply with OK.")]))
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        hint = ""
        if "Personal Access Tokens are not supported" in msg:
            hint = (" -- Copilot rejected the PAT under this integration id. Use a fine-grained PAT owned by your "
                    "personal account with the 'Copilot Requests' permission (sent as 'copilot-sdk'), or set "
                    "GITHUB_COPILOT_INTEGRATION_ID; --forget-token to re-enter.")
        elif "model_not_supported" in msg:
            hint = " -- your Copilot plan/token does not offer this model; pick another with --model."
        elif "401" in msg or "unauthorized" in msg.lower():
            hint = " -- token rejected; re-run with --forget-token."
        raise TdsError(f"model check failed for {getattr(llm, 'model', llm)}: {msg[:300]}{hint}") from e
