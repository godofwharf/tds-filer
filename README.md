# tds-filer

CLI that automates the local part of filing the quarterly **Form 144** TDS statement (TDS on
payments to non-residents; formerly Form 27Q). This version was built for one tenant
paying rent to a non-resident landlord.

It reads the Challan Receipt PDFs, builds the statement text file in Protean's FVU 1.2 layout,
checks it against the official format spec, runs Protean's FVU without opening its window,
and produces the zip you upload.

Uploading and e-verifying stay manual. The Income Tax Department has no public API for TDS
statements: its ERI APIs cover ITR filing only and require approval as an intermediary.

## Requirements
- macOS (challan text is read with PDFKit via `osascript`), Python ≥ 3.11 (standard library only)
- [uv](https://docs.astral.sh/uv/getting-started/installation/) (`brew install uv`)
- Java plus Protean's **TDS_STANDALONE_FVU_1.2** folder, and the Form 144 file format spreadsheet
  (`Form Number 144-27Q - Q1 to Q4_*.xlsx`), both from Protean's e-TDS download page

## Installation

### One-line install (dependencies + agent skill)
```sh
curl -fsSL https://raw.githubusercontent.com/godofwharf/tds-filer/master/install.sh | bash
```
[`install.sh`](install.sh) does the following:
- checks for macOS;
- installs **uv** and **Node.js** if they're missing (with Homebrew if present);
- checks for **Java**;
- checks that GitHub is reachable;
- runs `uvx` once so the `tds` CLI is built and cached;
- installs the **tds-filing** skill for Claude Code and OpenCode via `npx skills`.

It doesn't download Protean's FVU or create your config; it prints those next steps at the end.
To review the script before running it, download it to a file first and run it with `bash install.sh`.

Options are passed as environment variables on the `bash` side of the pipe, e.g. `… | TDS_SKILL_SCOPE=project bash`:

| Variable | Default | Meaning |
|---|---|---|
| `TDS_SKILL_SCOPE` | `global` | `global` (all projects) or `project` (current directory) |
| `TDS_AGENTS` | `claude-code opencode` | agents to install the skill for |
| `TDS_REF` | `master` | branch, tag or commit of tds-filer to use |
| `DO_NOT_TRACK` | unset | `1` turns off the `skills` CLI telemetry |

### CLI
`uvx` can run the CLI straight from the git repository, with no clone and no install:
```sh
uvx --from git+https://github.com/godofwharf/tds-filer tds --help
uvx --from git+https://github.com/godofwharf/tds-filer@v0.1.0 tds status    # pin a tag/branch/commit
uvx --refresh --from git+https://github.com/godofwharf/tds-filer tds status # pick up new commits (uvx caches builds)
```
To install it permanently so `tds` is on your PATH:
```sh
uv tool install git+https://github.com/godofwharf/tds-filer
uv tool upgrade tds-filer        # later
```

From a checkout: `uv run tds …`.

### First-time configuration
```sh
tds init        # writes ~/Documents/TDS Filing/tds_config.json (mode 0600) from the example
# edit the config: deductor, deductee, rent, rate, codes, FVU paths
tds init        # re-run: checks paths/java, pins FVU jar sha256 hashes
```
Personal data lives **only** in the config and quarter folders, outside this repo. Use `--config` or
`$TDS_CONFIG` to point at a different config file.

### Agent skill (Claude Code / OpenCode)
`skills/tds-filing/` is an [Agent Skill](https://agentskills.io) that teaches an agent the quarterly workflow.
Its `scripts/tds` wrapper runs the CLI through `uvx` from this repo, so the agent doesn't need a local install.
`AGENTS.md` covers working *on* the code.

Install it with the [`skills`](https://github.com/vercel-labs/skills) CLI (needs Node.js), straight from GitHub:
```sh
npx skills add godofwharf/tds-filer --skill tds-filing -g -a claude-code -a opencode -y
```
This makes the skill available in every project for Claude Code and OpenCode. Leave out `-g` to install it
for the current project only.

Update or remove it later:
```sh
npx skills update tds-filing
npx skills remove tds-filing -g
```

**Check it:** start the agent and ask "prepare my TDS filing for 2026-27 Q3". The skill runs the CLI through
`uvx`, so the machine needs uv.

## Quarterly workflow
1. Pay TDS monthly (Challan 281) and save each `…SBIN_ChallanReceipt.pdf` into the filing root.
2. After the quarter, download the CSI file from the portal into the quarter folder
   (`2026-27 Q2` → `2026-Q3/`, using the existing calendar-quarter naming).
3. Run:
   ```sh
   uv run tds run 2026-27 Q3
   ```
   This runs **extract → build → check → validate → package** and stops at the first failure:
   | Step | What it does |
   |---|---|
   | `extract` | moves this quarter's challan PDFs into the folder, parses them, writes `challans.json` and `Challan Details` |
   | `build` | writes `144RQn.txt` (previous-quarter RRR goes into BH#9 and sets BH#52=Y) |
   | `check` | checks challans, the CSI file, every field against the official spec, and totals across records |
   | `validate` | archives earlier outputs to `old_run/`, runs FVU without its window, parses `.err` and the stats report |
   | `package` | checks the FVU output (all challans matched, totals, RRR present), then zips only the `.fvu` |
4. Upload the zip on incometax.gov.in (Form 144, Regular), e-verify, then:
   ```sh
   uv run tds record-rrr 2026-27 Q3 <15-digit RRR>
   ```
### Optional: fetch inputs from the portal (`tds fetch`)
Steps 1–2 and the previous quarter's RRR can be collected by a [browser-use](https://github.com/browser-use/browser-use)
agent. The model is called through [pydantic-ai](https://ai.pydantic.dev), so any pydantic-ai provider works via
`--model provider:name`. The default, `github-copilot:claude-sonnet-5.5`, uses your GitHub Copilot subscription: on first use you are
asked for a Copilot token (input hidden): a fine-grained PAT owned by your personal account with the
**Copilot Requests** account permission (sent with integration id `copilot-sdk`), or an OAuth `gho_` token. It is stored in `~/.config/tds-filer/copilot_token` (0600) and
reused; `GITHUB_COPILOT_API_KEY` overrides it and `--forget-token` deletes it. No Anthropic/OpenAI key needed. Others: `anthropic:…`
(`ANTHROPIC_API_KEY`), `openai:…` (`OPENAI_API_KEY`), `ollama:…` (local). Which Copilot model ids work depends on your
plan and token (a `gh` CLI token got `model_not_supported` for Claude ids in testing).
```sh
uv sync --extra portal && uvx browser-use install
uv run tds fetch 2026-27 Q3                 # --what challans,csi,rrr (default: all)
```
- A visible Chrome window opens with a dedicated profile (`<filing root>/.browser-profile`, 0700). **You log in
  yourself** (password/OTP/captcha); the CLI waits for Enter. The session is reused next time.
- The browser is limited to `*.incometax.gov.in`; the LLM gets page text only (no screenshots), and the TAN is
  passed as a masked secret. The agent is told to only view and download, never submit, pay, upload or e-verify.
- Downloads go into the quarter folder; `extract` runs on them, and a fetched RRR is recorded only after you
  confirm (or with `-y`); an existing different RRR is never overwritten.
- Every run is logged to `<filing root>/.fetch-logs/<TY>-Q<n>-<timestamp>/` (0700): `steps.md` (per-step goals,
  actions, errors, timings + aggregates), `history.json` (browser-use), `conversation/` (full model input/output per
  step), `llm_calls.jsonl` (latency, tokens, validation errors) and `screenshots/` (moved out of `$TMPDIR`).
  These contain portal data; they never leave the filing root. `tds fetch-report [--full] [run]` summarises a run.
- Telemetry is disabled (`ANONYMIZED_TELEMETRY=false`). Page text is sent to your model provider (Copilot by default).

`uv run tds status` shows progress per quarter. Each step can also be run on its own (`tds check 2026-27 Q3`, etc.);
`-v` also lists the checks that passed.

## Checks
- **Challans:** one per month of the quarter; TAN, minor head 200 and section code match the config;
  tax+surcharge+cess = rent × rate; no interest/fee/penalty (warning only); CINs unique.
- **CSI file:** TAN matches; it's stamped for the configured FVU version (otherwise FVU fails with T-FV-3175);
  downloaded after the last challan.
- **Spec** (parsed from Protean's xlsx): field counts, mandatory fields filled, "no value" fields empty, sizes,
  DDMMYYYY dates, amounts written as `1000.00`, rate as `31.2000`, remittance code valid for the section,
  country and state codes.
- **Across records:** line numbers in sequence, challan count and totals in the batch header, challan-to-deductee
  links, deductee dates inside the quarter, TDS = amount × rate, file matches the challan PDFs.
- **Previous quarter:** if its RRR is recorded, BH#52=Y and BH#9 = that RRR.
- **After FVU:** no fresh `.err`; `.fvu` is newer than `.txt` and carries the RRR; stats show matched = N,
  unmatched = 0, all PANs valid.

## FVU command-line mode
Found by disassembling `com.tin.FVU.FVU.main` (not documented by Protean):
```
cd <FVU dir> && java -cp "./*" com.tin.FVU.FVU <in.txt> <out.err> <out.fvu> 0 <version> 0 <challan.csi>
```
It must run from the FVU folder (it loads `e-mudhra.cer` by relative path), and it can't use
`-Djava.awt.headless` because it creates a Swing frame. The JVM doesn't exit by itself, so the tool polls for
the output files and then stops the process.

## Known decisions (from FY 2026-27 Q2)
- FVU 1.2 dropped remittance code `08` (rent). Rent under section 1057 uses `99` (Other income).
- CD fields 24/25 (Interest/Others allocation) must be written as `0.00`.
- The statement PDF from FVU shows "Previous receipt number: NA" even when BH#9 is set; the `.fvu` file does carry the RRR.

## Tests
```sh
PYTHONPATH=src python3 -m unittest discover -s tests
```
