# AGENTS.md

Guidance for coding agents working **on** this repository. To *use* the tool for filing, load the skill in
`skills/tds-filing/SKILL.md`.

## What this is
A CLI, `tds`, that builds, checks and packages India's quarterly **Form 144** TDS statement (formerly 27Q) in
Protean's FVU 1.2 file format, then runs Protean's FVU (a Java app) without its window. Uploading to the portal
is deliberately out of scope: there is no public API.

## Layout
```
src/tds_filer/
  cli.py        argparse entry point; subcommands init/status/extract/build/check/validate/package/run/record-rrr/fetch
  config.py     Config (JSON), Quarter (tax-year math, due dates, folder label), TdsError
  challan.py    challan receipt PDF -> text (osascript + PDFKit) -> Challan
  csi.py        CSI header parse (TAN, FVU versions it's stamped for)
  spec.py       parses Protean's format xlsx with zipfile + regex: field rules, remittance/country/state codes
  builder.py    FH/BH/CD/DD records, positional (1-based field numbers, as in the spec)
  checks.py     Report/Finding; challan, CSI, per-field spec, cross-record, previous-RRR and post-FVU checks
  fvu.py        integrity check (permissions + sha256 pins), headless FVU run, .err/stats parsing
  package.py    zip containing only the .fvu
  portal.py     optional [portal] extra: browser-use agent driven by pydantic-ai for `tds fetch` (lazy import; manual login); run logs in <filing root>/.fetch-logs
  portal_script.py  default `tds fetch --mode script`: selector-driven steps (browser-use Actor API) + per-step checks; guarded agent fallback per failed step
  tds_config.example.json   synthetic example (packaged; used by `tds init`)
skills/tds-filing/          agent skill + scripts/tds wrapper (uvx from git)
tests/test_tds.py           unittest, synthetic data only
```

## Commands
```sh
PYTHONPATH=src python3 -m unittest discover -s tests   # tests (must pass before committing)
uv run tds --help                                       # run from the checkout
uv build                                                # wheel must contain tds_config.example.json
```
To try the full pipeline against real data, **copy** the quarter folder to a temp dir, point a copied config's
`filing_root` at it (`--config`), run there, and delete the temp dir afterwards. Never run `build`/`validate`
against the user's real folder unless they ask; it overwrites `144RQn.txt` and moves outputs to `old_run/`.

## Hard rules
- **No personal data in the repo, ever:** no PAN/TAN, names, addresses, phone numbers, emails, challan/CIN/RRR
  numbers, or real rent/TDS figures. Tests and the example config use synthetic values (rent 100000,
  TDS 31200, country 15). Before every commit:
  `git grep -nP '\b[A-Z]{5}[0-9]{4}[A-Z]\b|\b[A-Z]{4}[0-9]{5}[A-Z]\b|\b[0-9]{15}\b'` must show only the
  synthetic values. `.gitignore` blocks filing artifacts; don't weaken it.
- **Standard library only** (Python >= 3.11). No runtime dependencies. Exception: `portal.py` may use the
  optional `[portal]` extra (browser-use + pydantic-ai-slim; `PydanticAIChat` adapts pydantic-ai models to browser-use), imported lazily inside functions; nothing else may import it.
- Portal automation must never log in, submit, pay, upload or e-verify. This is enforced by `portal.guarded_tools`
  (excluded actions + `check_action` deny lists); never weaken it, and add a test for any new allow/deny label; keep `use_vision=False`,
  `allowed_domains`, the masked TAN, and telemetry off. Every scripted click/fill in `portal_script.Driver`
  also goes through `check_action`. Tests mock `portal_script.fetch`/`portal.fetch`; never hit the real portal.
  Scripted steps are tested against the synthetic fixture `tests/fixtures/portal/index.html`
  (`TDS_LIVE=1`, headless Chromium); update it when portal selectors change.
- **Field numbers are 1-based**, matching the spec xlsx. In code: `f[n - 1]` / `_rec(kind, {n: value})`.
  Record lengths: FH 18, BH 72, CD 30, DD 45 (`builder.COUNTS`); `checks` takes the authoritative counts from
  the spec.
- Amount formats: INTEGER amount fields are `1234.00`, DECIMAL `1234.56`, rate `31.2000`, dates `DDMMYYYY`.
- A tax judgement (rate, DTAA, remittance code, section) is config, never code.
- A new check: add it to `checks.py` with a message that says how to fix the problem; add a unit test that
  feeds it a broken file.

## FVU quirks (verified with FVU 1.2; see fvu.py)
- CLI form: `java -cp "./*" com.tin.FVU.FVU <in.txt> <out.err> <out.fvu> 0 <version> 0 <x.csi>`, with
  **cwd = the FVU folder** (it loads `e-mudhra.cer` by relative path). `-Djava.awt.headless=true` breaks it
  (it creates a JFrame).
- The JVM never exits after validating: poll for `.fvu` + `.html` + statement PDF (or `.err`) and terminate it.
- The `.fvu` contains a per-run hash, a timestamped barcode and an encrypted block, so two runs on the same
  input differ. Compare the `.txt` and the readable summary lines, not the whole `.fvu`.
- A CSI stamped for a newer FVU makes FVU fail with T-FV-3175.
- FVU 1.2 dropped remittance code `08`; CD#24/#25 (allocations) are mandatory; BH#9/#52 carry the previous
  quarter's RRR.
- The FVU-generated statement PDF prints "Previous receipt number: NA" even when BH#9 is set (known quirk).

## Releasing
Bump `version` in `pyproject.toml`, then tag (`git tag vX.Y.Z && git push --tags`). Users can pin with
`uvx --from git+https://github.com/godofwharf/tds-filer@vX.Y.Z tds …`.
