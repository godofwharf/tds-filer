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
- Java plus Protean's **TDS_STANDALONE_FVU_1.2** folder, and the Form 144 file format spreadsheet
  (`Form Number 144-27Q - Q1 to Q4_*.xlsx`), both from Protean's e-TDS download page

## Setup
```sh
uv run tds init        # writes ~/Documents/TDS Filing/tds_config.json from the example
# edit the config: deductor, deductee, rent, rate, codes, paths
uv run tds init        # re-run to verify FVU/spec/java paths
```
Personal data lives **only** in the config and quarter folders, outside this repo. Use `--config` or
`$TDS_CONFIG` to point at a different config file.

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
