"""`tds` command-line interface: one entry point for the quarterly Form 144 workflow."""

from __future__ import annotations

import argparse
import re
import shutil
import sys
from datetime import date, datetime
from pathlib import Path

from . import builder, challan, checks, csi, fvu, package, spec
from .config import Config, Quarter, TdsError, expand, DEFAULT_CONFIG


def _ctx(args) -> tuple[Config, Quarter, Path]:
    cfg = Config.load(args.config)
    qt = Quarter.parse(args.fy, args.q)
    return cfg, qt, cfg.quarter_dir(qt)


def _header(title: str) -> None:
    print(f"\n== {title}")


def _finish(rep: checks.Report, verbose: bool) -> None:
    rep.print(verbose)
    if rep.errors:
        raise TdsError(f"{len(rep.errors)} check(s) failed; fix the above and re-run")


def cmd_init(args) -> None:
    dest = expand(args.config or DEFAULT_CONFIG)
    if dest.exists():
        print(f"Config already exists: {dest}")
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(Path(__file__).with_name("tds_config.example.json"), dest)
        dest.chmod(0o600)
        print(f"Wrote {dest} from example; edit deductor/deductee details before use.")
        return
    cfg = Config.load(str(dest))
    ok = True
    for label, p in (("FVU dir", cfg.fvu_dir), ("format spec", cfg.spec_xlsx), ("filing root", cfg.filing_root)):
        exists = p.exists()
        ok &= exists
        print(f"  [{'OK' if exists else 'MISSING':7}] {label}: {p}")
    print(f"  [{'OK' if shutil.which('java') else 'MISSING':7}] java on PATH")
    if not ok:
        raise TdsError("Fix missing paths in config")
    if not cfg.data["utility"].get("sha256"):
        cfg.data["utility"]["sha256"] = fvu.pin_hashes(cfg)
        cfg.save()
        print(f"  pinned sha256 of {len(cfg.data['utility']['sha256'])} FVU jars in config")
    fvu.verify_integrity(cfg)
    print("  [OK     ] FVU files not group/world-writable; hashes match pins")


def cmd_extract(args) -> list[challan.Challan]:
    cfg, qt, qdir = _ctx(args)
    _header(f"extract challans for {qt.key} -> {qdir}")
    ch = challan.collect(cfg.filing_root, qdir, qt)
    challan.save(ch, qdir)
    for c in ch:
        print(f"  {c.dt:%d-%b-%Y}  BSR {c.bsr}  No {c.challan_no}  tax {c.tax} + cess {c.cess} = {c.total}  ({c.cin})")
    rep = checks.Report()
    checks.check_challans(rep, cfg, qt, ch)
    _finish(rep, args.verbose)
    return ch


def cmd_build(args) -> Path:
    cfg, qt, qdir = _ctx(args)
    _header(f"build {qt.stem}.txt")
    ch = challan.load(qdir)
    created = datetime.strptime(args.date, "%d%m%Y").date() if getattr(args, "date", None) else None
    txt = builder.write(qdir, qt, builder.build(cfg, qt, ch, created))
    print(f"  wrote {txt}")
    return txt


def cmd_check(args) -> None:
    cfg, qt, qdir = _ctx(args)
    _header(f"pre-FVU checks for {qt.key}")
    sp = spec.load(cfg.spec_xlsx)
    ch = challan.load(qdir)
    rep = checks.Report()
    checks.check_challans(rep, cfg, qt, ch)
    try:
        checks.check_csi(rep, cfg, csi.parse(csi.find(qdir)), ch)
    except TdsError as e:
        rep.add(checks.ERROR, "csi", str(e))
    txt = qdir / f"{qt.stem}.txt"
    if rep.check(txt.exists(), "file", f"{txt.name} exists"):
        checks.check_file(rep, cfg, qt, sp, txt.read_text(), ch)
    _finish(rep, args.verbose)


def cmd_validate(args) -> None:
    cfg, qt, qdir = _ctx(args)
    _header("FVU validation")
    old = fvu.archive_previous(qdir, qt)
    if old:
        print(f"  archived previous outputs to {old.relative_to(qdir)}")
    fvu.run(cfg, qt, qdir, csi.find(qdir))
    _verify_fvu(cfg, qt, qdir, args.verbose)


def _verify_fvu(cfg: Config, qt: Quarter, qdir: Path, verbose: bool) -> None:
    rep = checks.Report()
    n = len(challan.load(qdir))
    checks.check_fvu_outputs(rep, qt, qdir, n, cfg.rrr(qt.previous()))
    _finish(rep, verbose)


def cmd_package(args) -> Path:
    cfg, qt, qdir = _ctx(args)
    _header("package")
    _verify_fvu(cfg, qt, qdir, args.verbose)
    z = package.make_zip(qdir, qt)
    print(f"  ready to upload: {z}")
    return z


def cmd_record_rrr(args) -> None:
    cfg = Config.load(args.config)
    qt = Quarter.parse(args.fy, args.q)
    if not re.fullmatch(r"\d{15}", args.rrr):
        raise TdsError("RRR/token must be exactly 15 digits (from the upload acknowledgement)")
    old = cfg.rrr(qt)
    if old and old != args.rrr and not args.force:
        raise TdsError(f"{qt.key} already has RRR {old}; use --force to overwrite")
    cfg.set_rrr(qt, args.rrr)
    print(f"Recorded RRR {args.rrr} for {qt.key}")


def cmd_fetch(args) -> None:
    from . import portal
    if getattr(args, "forget_token", False) and portal.forget_copilot_token():
        print(f"Deleted stored Copilot token {portal.TOKEN_FILE}")
    cfg, qt, qdir = _ctx(args)
    what = [w.strip() for w in args.what.split(",") if w.strip()]
    bad = set(what) - set(portal.WHAT)
    if bad:
        raise TdsError(f"--what: unknown {sorted(bad)}; choose from {','.join(portal.WHAT)}")
    _header(f"fetch {','.join(what)} for {qt.key} -> {qdir} (browser; you log in manually)")
    if args.mode == "script":
        from . import portal_script
        res = portal_script.fetch(cfg, qt, qdir, what, args.model, fallback=not args.no_fallback)
        for st in res.steps:
            status = "ok" if st.ok else "FAILED"
            print(f"  step {st.name:9} {status:6} via {st.via:6} {st.seconds:6.1f}s  {st.error if not st.ok or st.via == 'agent' else ''}")
    else:
        res = portal.fetch(cfg, qt, qdir, what, args.model, record=args.mode == "record")
    print(f"  new files: {', '.join(res.new_files) or 'none'}")
    for b in res.blocked:
        print(f"  SAFETY GUARD blocked: {b}")
    if "challans" in what:
        if not any("ChallanReceipt" in f for f in res.new_files):
            print("  WARNING: no new challan receipts downloaded; checking what is already in the folder")
        cmd_extract(args)
    if "csi" in what:
        try:
            print(f"  CSI: {csi.find(qdir).name}")
        except TdsError as e:
            print(f"  WARNING: {e}")
    for key, rrr in res.rrr.items():
        prev = qt.previous()
        old = cfg.rrr(prev)
        if old == rrr:
            print(f"  RRR for {key} already recorded: {rrr}")
        elif old:
            print(f"  WARNING: portal RRR {rrr} for {key} differs from recorded {old}; not changed")
        elif args.yes or input(f"  Record RRR {rrr} for {key}? [y/N] ").strip().lower() == "y":
            cfg.set_rrr(prev, rrr)
            print(f"  Recorded RRR {rrr} for {key}")
    if "rrr" in what and not res.rrr:
        print("  WARNING: no RRR reported by the portal agent")
    if res.log_dir:
        print(f"  run log: {res.log_dir}/steps.md (tds fetch-report for a summary)")


def cmd_fetch_report(args) -> None:
    import json
    from . import portal
    cfg = Config.load(args.config)
    if args.path:
        d = Path(args.path).expanduser()
    else:
        runs = sorted((cfg.filing_root / ".fetch-logs").glob("*/history.json"))
        if not runs:
            raise TdsError("no fetch runs logged yet")
        d = runs[-1].parent
    h = d / "history.json" if d.is_dir() else d
    text = portal.summarize_history(json.loads(h.read_text()))
    print(f"{h}\n" + (text if args.full else text.split("\n## Steps\n")[0]))


def _checklist(cfg: Config, qt: Quarter, qdir: Path) -> None:
    today = date.today()
    days = (qt.due_date - today).days
    due = f"due {qt.due_date:%d %b %Y}" + (f" ({days} days left)" if days >= 0 else f" (OVERDUE by {-days} days: late fee Rs 200/day)")
    print(f"""
== Manual steps ({due})
  1. incometax.gov.in -> login with TAN {cfg.deductor['tan']}
  2. e-File -> Income Tax Forms -> File Income Tax Forms -> Form 144, TY {qt.fy}, Q{qt.q}, Regular
  3. Upload {qdir / qt.zip_name}
  4. E-verify (Aadhaar OTP / EVC / DSC); save the acknowledgement into {qdir.name}/
  5. tds record-rrr {qt.fy} Q{qt.q} <15-digit RRR>
  6. Later: check processing on TRACES and download the TDS certificate for the deductee""")


def cmd_run(args) -> None:
    cfg, qt, qdir = _ctx(args)
    print(f"Form 144 {qt.key}  ({qt.start:%d-%b-%Y} .. {qt.end:%d-%b-%Y})  folder {qdir}")
    cmd_extract(args)
    cmd_build(args)
    cmd_check(args)
    if args.no_fvu:
        print("\n--no-fvu: stopping before FVU")
        return
    cmd_validate(args)
    cmd_package(args)
    _checklist(cfg, qt, qdir)


def cmd_status(args) -> None:
    cfg = Config.load(args.config)
    root = cfg.filing_root
    today = date.today()
    fy0 = today.year if today.month >= 4 else today.year - 1
    print(f"{'quarter':12} {'folder':10} {'challans':8} {'csi':4} {'txt':4} {'fvu':4} {'zip':4} RRR")
    for fy in (fy0 - 1, fy0):
        for q in (1, 2, 3, 4):
            qt = Quarter(fy, q)
            if qt.start > today or qt.fy_start < 2026:  # Form 144 starts with TY 2026-27
                continue
            d = cfg.quarter_dir(qt)
            has = lambda pat: "yes" if d.exists() and any(d.glob(pat)) else "-"  # noqa: E731
            n = len(list(d.glob("*ChallanReceipt*.pdf"))) if d.exists() else 0
            print(f"{qt.key:12} {d.name:10} {n:<8} {has('*.csi'):4} {has(qt.stem + '.txt'):4} "
                  f"{has(qt.stem + '.fvu'):4} {has(qt.zip_name):4} {cfg.rrr(qt) or '-'}")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="tds", description="Quarterly Form 144 (TDS on payments to non-residents) workflow")
    ap.add_argument("--config", help=f"config path (default $TDS_CONFIG or {DEFAULT_CONFIG})")
    ap.add_argument("-v", "--verbose", action="store_true", help="show passing checks too")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def q(name, fn, help_):
        p = sub.add_parser(name, help=help_)
        p.add_argument("fy", help="tax year, e.g. 2026-27")
        p.add_argument("q", help="quarter, e.g. Q2")
        p.set_defaults(fn=fn)
        return p

    sub.add_parser("init", help="create config / verify environment").set_defaults(fn=cmd_init)
    sub.add_parser("status", help="per-quarter progress").set_defaults(fn=cmd_status)
    q("extract", cmd_extract, "move+parse challan PDFs into the quarter folder")
    q("build", cmd_build, "generate 144RQn.txt").add_argument("--date", help="file creation date DDMMYYYY (default today)")
    q("check", cmd_check, "run all pre-FVU assertions")
    q("validate", cmd_validate, "run FVU headlessly and verify its outputs")
    q("package", cmd_package, "verify FVU outputs and create the upload zip")
    r = q("run", cmd_run, "extract -> build -> check -> validate -> package")
    r.add_argument("--date", help="file creation date DDMMYYYY (default today)")
    r.add_argument("--no-fvu", action="store_true", help="stop after checks")
    f = q("fetch", cmd_fetch, "download challans/CSI/previous RRR from the portal (needs [portal] extra)")
    f.add_argument("--what", default=",".join(("challans", "csi", "rrr")), help="comma list: challans,csi,rrr")
    f.add_argument("--model", default="github-copilot:claude-sonnet-5.5",
                   help="pydantic-ai model 'provider:name' (default: GitHub Copilot; token asked once and stored in "
                        "~/.config/tds-filer/copilot_token; e.g. anthropic:claude-sonnet-4-5, openai:gpt-4.1, ollama:qwen3)")
    f.add_argument("-y", "--yes", action="store_true", help="record a newly fetched RRR without prompting")
    f.add_argument("--mode", choices=("script", "agent", "record"), default="script",
                   help="script (default): deterministic steps, LLM agent only as per-step fallback; agent: LLM drives "
                        "the whole flow; record: agent + capture selectors/page outlines/XHR endpoints")
    f.add_argument("--no-fallback", action="store_true", help="script mode: never use the LLM")
    f.add_argument("--forget-token", action="store_true", help="delete the stored Copilot token (asks again)")
    fr = sub.add_parser("fetch-report", help="summarise a logged `tds fetch` run (default: latest)")
    fr.add_argument("path", nargs="?", help="run dir or history.json")
    fr.add_argument("--full", action="store_true", help="include every step")
    fr.set_defaults(fn=cmd_fetch_report)
    rr = q("record-rrr", cmd_record_rrr, "save the 15-digit RRR/token after upload")
    rr.add_argument("rrr")
    rr.add_argument("--force", action="store_true")

    args = ap.parse_args(argv)
    try:
        args.fn(args)
    except TdsError as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
