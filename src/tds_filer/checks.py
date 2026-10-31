"""Assertions run before and after FVU. Each check yields Findings; any ERROR blocks the workflow."""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date, datetime
from pathlib import Path

from .challan import Challan
from .config import Config, Quarter
from .csi import Csi
from .spec import Spec

ERROR, WARN, OK = "ERROR", "WARN", "OK"


@dataclass
class Finding:
    level: str
    area: str
    msg: str

    def __str__(self) -> str:
        return f"[{self.level:5}] {self.area}: {self.msg}"


class Report:
    def __init__(self) -> None:
        self.items: list[Finding] = []

    def add(self, level: str, area: str, msg: str) -> None:
        self.items.append(Finding(level, area, msg))

    def check(self, cond: bool, area: str, msg: str, level: str = ERROR) -> bool:
        self.add(OK if cond else level, area, msg)
        return cond

    @property
    def errors(self) -> list[Finding]:
        return [f for f in self.items if f.level == ERROR]

    @property
    def warnings(self) -> list[Finding]:
        return [f for f in self.items if f.level == WARN]

    def print(self, verbose: bool = False) -> None:
        for f in self.items:
            if verbose or f.level != OK:
                print("  " + str(f))
        n_ok = sum(f.level == OK for f in self.items)
        print(f"  {n_ok} passed, {len(self.warnings)} warnings, {len(self.errors)} errors")


def expected_tds(rent: int, rate_percent: float) -> int:
    return int(round(rent * rate_percent / 100))


def check_challans(r: Report, cfg: Config, qt: Quarter, ch: list[Challan]) -> None:
    a = "challans"
    want = cfg.data.get("challans_per_quarter", 3)
    r.check(len(ch) == want, a, f"{len(ch)} challan(s) found, expected {want}")
    r.check(len({c.cin for c in ch}) == len(ch), a, "CINs are unique")
    months = sorted((c.dt.year, c.dt.month) for c in ch)
    r.check(len(set(months)) == len(months), a, "one challan per month")
    missing = [f"{y}-{m:02d}" for y, m in qt.months if (y, m) not in months]
    r.check(not missing, a, f"every month of {qt.key} has a challan" + (f" (missing {missing})" if missing else ""))
    for c in ch:
        tag = f"{c.challan_no} ({c.dt:%d-%b-%Y})"
        r.check(qt.contains(c.dt), a, f"{tag} dated within quarter")
        r.check(c.tan == cfg.deductor["tan"], a, f"{tag} TAN {c.tan} matches config")
        r.check(c.minor_head == cfg.data.get("minor_head", "200"), a, f"{tag} minor head {c.minor_head}")
        r.check(c.section_code == cfg.deductee["section_code"], a, f"{tag} section code {c.section_code}")
        rent = cfg.rent_for(c.dt.year, c.dt.month)
        exp = expected_tds(rent, cfg.deductee["rate_percent"])
        r.check(c.tax + c.surcharge + c.cess == exp, a,
                f"{tag} tax+surcharge+cess {c.tax + c.surcharge + c.cess} == rent {rent} x {cfg.deductee['rate_percent']}% = {exp}")
        r.check(c.interest + c.fee + c.penalty + c.others == 0, a,
                f"{tag} no interest/fee/penalty paid", level=WARN)


def check_csi(r: Report, cfg: Config, csi: Csi, ch: list[Challan]) -> None:
    a = "csi"
    r.check(csi.tan == cfg.deductor["tan"], a, f"{csi.path.name} TAN {csi.tan}")
    r.check(cfg.version in csi.versions, a,
            f"CSI stamped for FVU {csi.versions}, includes utility {cfg.version} "
            "(else FVU fails with T-FV-3175: re-download CSI or upgrade FVU)")
    if ch:
        mdate = date.fromtimestamp(csi.path.stat().st_mtime)
        last = max(c.dt for c in ch)
        r.check(mdate >= last, a, f"CSI downloaded {mdate} on/after last challan {last}")


_DATE = re.compile(r"\d{8}")


def _valid_date(v: str) -> bool:
    try:
        datetime.strptime(v, "%d%m%Y")
        return True
    except ValueError:
        return False


def check_file(r: Report, cfg: Config, qt: Quarter, spec: Spec, text: str, ch: list[Challan] | None = None) -> None:
    a = "file"
    if not r.check(text.endswith("\n") and "\r" not in text, a, "LF line endings, trailing newline"):
        pass
    lines = text.rstrip("\n").split("\n")
    recs = [l.split("^") for l in lines]
    kinds = [f[1] if len(f) > 1 else "?" for f in recs]
    r.check(kinds[:2] == ["FH", "BH"] and all(k in ("CD", "DD") for k in kinds[2:]), a, f"record order {kinds}")
    for i, f in enumerate(recs, 1):
        r.check(f[0] == str(i), a, f"line {i} has running line number")

    # Per-field rules from the official spec
    for i, f in enumerate(recs, 1):
        kind = f[1] if len(f) > 1 else "?"
        rules = spec.records.get(kind)
        if not rules:
            r.add(ERROR, a, f"line {i}: unknown record type {kind}")
            continue
        if not r.check(len(f) == spec.count(kind), a, f"line {i} {kind}: {len(f)} fields (spec {spec.count(kind)})"):
            continue
        for n, rule in rules.items():
            v = f[n - 1]
            where = f"line {i} {kind}#{n} {rule.name[:45]}"
            if rule.mandatory and not v:
                r.add(ERROR, a, f"{where}: mandatory but empty")
            if rule.must_be_empty and v:
                r.add(ERROR, a, f"{where}: must be empty, has {v!r}")
            if not v:
                continue
            if rule.size and rule.dtype in ("CHAR", "INTEGER", "DECIMAL") and len(v) > rule.size:
                r.add(ERROR, a, f"{where}: {v!r} longer than {rule.size}")
            if rule.dtype == "DATE" and not (_DATE.fullmatch(v) and _valid_date(v)):
                r.add(ERROR, a, f"{where}: {v!r} is not DDMMYYYY")
            if rule.dtype == "INTEGER" and not re.fullmatch(r"\d+(\.00)?", v):
                r.add(ERROR, a, f"{where}: {v!r} not an integer amount (use 1000.00 form)")
            if rule.dtype == "DECIMAL":
                pat = r"\d+\.\d{4}" if (kind, n) == ("DD", 28) else r"\d+\.\d{2}"
                if not re.fullmatch(pat, v):
                    r.add(ERROR, a, f"{where}: {v!r} wrong decimal format")
    r.add(OK, a, "per-field spec rules evaluated")

    fh, bh = recs[0], recs[1]
    cds = [f for f in recs if f[1] == "CD"]
    dds = [f for f in recs if f[1] == "DD"]
    tan = cfg.deductor["tan"]
    x = "cross"
    r.check(fh[7] == tan == bh[12], x, "TAN consistent (FH, BH, config)")
    r.check(fh[9] == cfg.data["utility"]["rpu_name"], x, f"FH utility name {fh[9]!r}", level=WARN)
    r.check((bh[15], bh[16], bh[17]) == (qt.ay_field, qt.fy_field, f"Q{qt.q}"), x,
            f"BH AY/TY/quarter = {bh[15]}/{bh[16]}/{bh[17]}")
    r.check(bh[4] == "144", x, "form number 144")
    r.check(int(bh[3]) == len(cds), x, f"BH challan count {bh[3]} == {len(cds)} CD records")
    cd_total = sum(float(c[11]) for c in cds)
    r.check(abs(float(bh[46]) - cd_total) < 0.005, x, f"BH total {bh[46]} == sum of challans {cd_total:.2f}")
    for c in cds:
        idx = c[3]
        mine = [d for d in dds if d[4] == idx]
        tag = f"challan #{idx} ({c[16]})"
        r.check(int(c[4]) == len(mine), x, f"{tag} deductee count {c[4]} == {len(mine)}")
        r.check(abs(float(c[20]) - sum(float(d[24]) for d in mine)) < 0.005, x, f"{tag} deposited == sum DD")
        r.check(abs(float(c[21]) - sum(float(d[23]) for d in mine)) < 0.005, x, f"{tag} deducted == sum DD")
        r.check(c[23] == c[8] and c[24] == c[10], x, f"{tag} interest/others allocation mirror fields 9/11")
        r.check(_valid_date(c[18]) and qt.contains(datetime.strptime(c[18], "%d%m%Y").date()), x,
                f"{tag} challan date in quarter", level=WARN)
        if ch:
            match = [k for k in ch if k.challan_no == c[16] and k.bsr == c[14] and k.date == c[18]]
            r.check(len(match) == 1 and abs(float(c[11]) - match[0].total) < 0.005, x,
                    f"{tag} matches challan receipt PDF (BSR, number, date, amount)")
    cd_ids = {c[3] for c in cds}
    for d in dds:
        tag = f"deductee line {d[0]}"
        r.check(d[4] in cd_ids, x, f"{tag} references existing challan #{d[4]}")
        r.check(d[7] == cfg.deductee["pan"], x, f"{tag} PAN matches config")
        for n in (19, 27):
            v = d[n - 1]
            r.check(_valid_date(v) and qt.contains(datetime.strptime(v, "%d%m%Y").date()), x,
                    f"{tag} field {n} date {v} within quarter (FVU 1.2 rule)")
        exp = expected_tds(int(float(d[19])), float(d[27]))
        r.check(int(float(d[23])) == exp, x, f"{tag} TDS {d[23]} == {d[19]} x {d[27]}% = {exp}")
        sec, code = d[14], d[15]
        if sec in spec.remittance:
            r.check(code in spec.remittance[sec], x,
                    f"{tag} nature of remittance {code} valid for section {sec} (allowed {sorted(spec.remittance[sec])})")
        if spec.countries:
            r.check(d[16] in spec.countries, x, f"{tag} country {d[16]} = {spec.countries.get(d[16], '?')}")
    if spec.states:
        r.check(bh[25] in spec.states, x, f"state code {bh[25]} = {spec.states.get(bh[25], '?')}")

    p = "previous"
    prev = cfg.rrr(qt.previous())
    if prev:
        r.check(re.fullmatch(r"\d{15}", prev) is not None, p, f"previous RRR {prev} is 15 digits")
        r.check(bh[51] == "Y" and bh[8] == prev, p, f"BH#52=Y and BH#9={prev} (previous quarter filed)")
    else:
        r.add(ERROR if cfg.data.get("rrr") else WARN, p,
              f"no RRR recorded for {qt.previous().key}; BH#52=N. If that quarter was filed, run "
              f"`tds record-rrr {qt.previous().fy} Q{qt.previous().q} <15-digit RRR>` and rebuild")


def check_fvu_outputs(r: Report, qt: Quarter, qdir: Path, n_challans: int, prev_rrr: str | None) -> None:
    from . import fvu

    a = "fvu"
    txt = qdir / f"{qt.stem}.txt"
    err = qdir / f"{qt.stem}.err"
    out = qdir / f"{qt.stem}.fvu"
    if err.exists() and err.stat().st_mtime >= txt.stat().st_mtime:
        r.add(ERROR, a, f"FVU reported errors:\n{fvu.format_errors(err)}")
        return
    if not r.check(out.exists(), a, f"{out.name} exists"):
        return
    r.check(out.stat().st_mtime >= txt.stat().st_mtime, a, f"{out.name} is newer than {txt.name}")
    body = out.read_text(errors="replace")
    r.check(re.search(r"\^FVU \d", body.splitlines()[0]) is not None, a, ".fvu header stamped by FVU")
    if prev_rrr:
        r.check(prev_rrr in body, a, f".fvu carries previous RRR {prev_rrr}")
    st = fvu.stats(qdir / f"{qt.stem}.html")
    if not r.check(st is not None, a, "statistics report parsed"):
        return
    r.check(st["challans"] == n_challans, a, f"challans in stats {st['challans']} == {n_challans}")
    r.check(st["matched"] == n_challans and st["unmatched"] == 0, a,
            f"CSI matched {st['matched']}, unmatched {st['unmatched']}")
    r.check(st["invalid_pan"] == 0 and st["pan_not_available"] == 0, a, "all deductee PANs valid")
    r.check(st["higher_rate"] == 0, a, "no higher-rate deductee records", level=WARN)
    r.check(st["form"] == "144" and st["quarter"] == f"Q{qt.q}" and st["tax_year"] == qt.fy, a,
            f"stats header: form {st['form']}, {st['tax_year']} {st['quarter']}")
