---
name: tds-filing
description: Prepare, validate and package the quarterly Indian TDS statement Form 144 (formerly 27Q; TDS on rent or other payments to a non-resident) using the tds-filer CLI and Protean's FVU. Use when the user asks to file or prepare TDS, Form 144/27Q, process challan receipts or CSI files, fix FVU errors (T-FV-*/T_FV_*), or record an RRR/token number.
compatibility: macOS (PDFKit via osascript), uv/uvx, Java, Protean FVU 1.2+
---

# TDS filing (Form 144) with tds-filer

Run every step through the CLI wrapper `scripts/tds` (path relative to this file; it runs
`uvx --from git+https://github.com/godofwharf/tds-filer tds …`). Do **not** build or edit the statement
`.txt` by hand: change the config and rebuild, so the checks always run.

## Facts to keep in mind
- Tax year `2026-27` = Apr 2026 – Mar 2027. Quarters: Q1 Apr–Jun (due 31 Jul), Q2 Jul–Sep (31 Oct),
  Q3 Oct–Dec (31 Jan), Q4 Jan–Mar (31 May). Late fee: Rs 200/day, capped at the TDS amount.
- Quarter folders are named after the **calendar** quarter of the first month: TY 2026-27 Q2 → `2026-Q3/`.
  Check `tds status` rather than guessing.
- Personal data lives only in the user's config (`~/Documents/TDS Filing/tds_config.json`, or `$TDS_CONFIG`)
  and the quarter folders. Don't copy it anywhere else, don't commit it, and don't repeat PAN/TAN in chat
  unless needed.

## Workflow
1. **Check the environment:** `scripts/tds init`. If anything is MISSING, see "Setup" below.
2. **See where things stand:** `scripts/tds status`.
3. **Make sure the inputs exist** (only the user can get these from the portal):
   - one `…ChallanReceipt.pdf` per month, in the filing root or the quarter folder
   - a fresh `.csi` file in the quarter folder (incometax.gov.in → e-Pay Tax → Payment History → Download CSI)
   - the previous quarter's RRR recorded in the config (`tds status` shows the RRR column). If it's missing,
     ask the user for the 15-digit RRR from the previous upload acknowledgement and run
     `scripts/tds record-rrr <FY> Q<n-1> <RRR>`.
   - Optional: if the `[portal]` extra is installed, `scripts/tds fetch <FY> Q<n>` opens a browser where the
     **user logs in themselves** and an agent downloads challans, CSI and the previous RRR. Only run it when the
     user asks; they must be at the keyboard to log in. It cannot upload, submit, pay or e-verify: a code-level guard blocks those actions.
4. **Run everything:** `scripts/tds run <FY> Q<n>` (e.g. `scripts/tds run 2026-27 Q3`).
   This runs extract → build → check → validate (FVU without its window, about 5 s) → package, and stops at
   the first failure. Add `-v` before the subcommand to see passing checks, or `--no-fvu` to stop after the checks.
   Individual steps: `extract`, `build`, `check`, `validate`, `package` (same `<FY> Q<n>` arguments).
5. **Report back** using the CLI output: the challans (date, BSR, number, amount), totals, matched/unmatched
   challans, the path to `<YYYY>_<YYYY+1>_144RQ<n>.zip`, the due date and any lateness.
6. **Hand over** the manual steps the CLI prints: upload on incometax.gov.in (Form 144, Regular), e-verify,
   save the acknowledgement. **Never attempt to log in, upload or e-verify yourself.**
7. **After the user uploads:** `scripts/tds record-rrr <FY> Q<n> <15-digit RRR>`.

## Decisions that belong to the user
Ask before changing any of these in the config; never infer them:
- rent amount or a one-off month (`deductee.rent_overrides`, e.g. `{"2026-11": 120000}`)
- the TDS rate, whether a DTAA rate applies (`rate_basis` B needs a tax residency certificate and Form 10F),
  the section code, the nature-of-remittance code, the deductee's country
- whether a previous quarter was really filed (this decides BH#52 Y/N)

## Fixing failures
| Symptom | Cause | Action |
|---|---|---|
| `T-FV-3175` | CSI file stamped for a newer FVU than the one installed | Install the matching FVU (see "Setup"), update the config |
| `T_FV_6546` / `T_FV_6548` | Interest/Others allocation not `0.00` | Bug in the builder; fix the code, don't patch the file |
| `T_FV_6423` nature of remittance | Code not allowed for the section in this FVU version | `tds check` lists the allowed codes; ask the user which one fits (rent → `99` in 1.2) |
| check `previous` ERROR | Previous quarter's RRR not recorded | Ask for the RRR, then `record-rrr` |
| check `challans` tax ≠ rent × rate | Rent changed, or the wrong amount was paid | Ask the user; set `rent_overrides`, or flag that a correction/extra challan is needed |
| `NumberFormatException` in the FVU log | Field layout doesn't match the FVU version | Compare against the spec xlsx; update the builder |
| FVU files writable / hash mismatch | Jar folder modified or permissions loosened | `chmod -R go-w <fvu_dir>`; re-download from Protean; drop `utility.sha256` and run `tds init` |

## Setup (first time, or when Protean releases a new FVU)
1. On Protean's e-TDS download page (`tinpan.proteantech.in` → Downloads → e-TDS → Regular), download
   `TDS_STANDALONE_FVU_<ver>.zip` and the Form 144 file-format xlsx. Unzip into e.g. `~/Documents/TDS_RPU_<ver>/`.
2. `chmod -R go-w ~/Documents/TDS_RPU_<ver>`.
3. In the config set `utility.version`, `utility.rpu_name`, `utility.fvu_dir`, `utility.spec_xlsx`, and remove
   `utility.sha256`.
4. `scripts/tds init` records new jar hashes and checks the paths.
5. Read the release notes (Key Features PDF) for layout changes. If FVU reports new errors, update
   `builder.py`/`checks.py` in the tds-filer repo (see its AGENTS.md) rather than editing statement files.
