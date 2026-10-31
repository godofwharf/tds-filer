"""Build the Form 144 (FVU 1.2 layout) statement text file."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from .challan import Challan
from .config import Config, Quarter, TdsError

COUNTS = {"FH": 18, "BH": 72, "CD": 30, "DD": 45}


def amt(x: float | int) -> str:
    return f"{x:.2f}"


def _rec(kind: str, values: dict[int, str]) -> str:
    f = [""] * COUNTS[kind]
    for n, v in values.items():
        f[n - 1] = str(v)
    return "^".join(f)


def build(cfg: Config, qt: Quarter, challans: list[Challan], created: date | None = None) -> str:
    if not challans:
        raise TdsError("No challans for this quarter")
    dr, de = cfg.deductor, cfg.deductee
    prev = cfg.rrr(qt.previous())
    total = sum(c.total for c in challans)
    created = created or date.today()
    a = dr["address"]
    lines = [
        _rec("FH", {1: 1, 2: "FH", 3: "NS1", 4: "R", 5: created.strftime("%d%m%Y"), 6: 1, 7: "D",
                    8: dr["tan"], 9: 1, 10: cfg.data["utility"]["rpu_name"]}),
        _rec("BH", {
            1: 2, 2: "BH", 3: 1, 4: len(challans), 5: "144", 9: prev or "",
            13: dr["tan"], 15: dr["pan"], 16: qt.ay_field, 17: qt.fy_field, 18: f"Q{qt.q}",
            19: dr["name"], 20: dr["country"], 21: a[0], 22: a[1], 23: a[2], 24: a[3], 25: a[4],
            26: dr["state_code"], 27: dr["pincode"], 28: dr["email"], 29: dr["phone_cc"], 30: dr["phone"],
            32: dr["type"], 33: dr["name"], 34: dr["designation"],
            35: a[0], 36: a[1], 37: a[2], 38: a[3], 39: a[4],
            40: dr["state_code"], 41: dr["pincode"], 42: dr["email"], 43: dr["country"],
            44: dr["phone_cc"], 45: dr["phone"], 47: amt(total), 52: "Y" if prev else "N", 59: dr["pan"],
        }),
    ]
    line = 3
    for i, c in enumerate(challans, 1):
        lines.append(_rec("CD", {
            1: line, 2: "CD", 3: 1, 4: i, 5: 1, 6: "N", 8: amt(c.total),
            9: amt(c.interest), 10: amt(c.fee), 11: amt(c.penalty + c.others), 12: amt(c.total),
            13: "C", 15: c.bsr, 17: c.challan_no, 19: c.date, 21: amt(c.total), 22: amt(c.total),
            23: c.minor_head, 24: amt(c.interest), 25: amt(c.penalty + c.others),
        }))
        rent = cfg.rent_for(c.dt.year, c.dt.month)
        lines.append(_rec("DD", {
            1: line + 1, 2: "DD", 3: 1, 4: 1, 5: i, 6: "O", 8: de["pan"], 9: de["name"],
            12: de["status"], 14: "N", 15: de["section_code"], 16: de["remittance_code"],
            17: de["country_code"], 18: "Y", 19: c.date, 20: amt(rent), 21: "N",
            24: amt(c.total), 25: amt(c.total), 27: c.date, 28: f"{de['rate_percent']:.4f}",
            30: de["rate_basis"],
        }))
        line += 2
    return "\n".join(lines) + "\n"


def write(qdir: Path, qt: Quarter, text: str) -> Path:
    p = qdir / f"{qt.stem}.txt"
    p.write_text(text)
    return p
