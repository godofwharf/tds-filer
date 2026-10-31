"""Configuration loading and tax-year/quarter arithmetic."""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass
from datetime import date
from pathlib import Path

DEFAULT_CONFIG = "~/Documents/TDS Filing/tds_config.json"

QUARTER_MONTHS = {1: (4, 5, 6), 2: (7, 8, 9), 3: (10, 11, 12), 4: (1, 2, 3)}
DUE_DATES = {1: (7, 31), 2: (10, 31), 3: (1, 31), 4: (5, 31)}


class TdsError(Exception):
    """A user-facing failure; message should say how to fix it."""


@dataclass(frozen=True)
class Quarter:
    fy_start: int  # 2026 for tax year 2026-27
    q: int

    @classmethod
    def parse(cls, fy: str, q: str) -> "Quarter":
        m = re.fullmatch(r"(\d{4})-(\d{2})", fy)
        if not m or (int(m.group(1)) + 1) % 100 != int(m.group(2)):
            raise TdsError(f"Tax year must look like 2026-27, got {fy!r}")
        qm = re.fullmatch(r"[Qq]?([1-4])", q)
        if not qm:
            raise TdsError(f"Quarter must be Q1..Q4, got {q!r}")
        return cls(int(m.group(1)), int(qm.group(1)))

    @property
    def fy(self) -> str:
        return f"{self.fy_start}-{(self.fy_start + 1) % 100:02d}"

    @property
    def key(self) -> str:
        return f"{self.fy}/Q{self.q}"

    @property
    def fy_field(self) -> str:
        return f"{self.fy_start}{(self.fy_start + 1) % 100:02d}"

    @property
    def ay_field(self) -> str:
        return f"{self.fy_start + 1}{(self.fy_start + 2) % 100:02d}"

    def year_of(self, month: int) -> int:
        return self.fy_start + 1 if month <= 3 else self.fy_start

    @property
    def months(self) -> list[tuple[int, int]]:
        return [(self.year_of(m), m) for m in QUARTER_MONTHS[self.q]]

    @property
    def start(self) -> date:
        y, m = self.months[0]
        return date(y, m, 1)

    @property
    def end(self) -> date:
        y, m = self.months[-1]
        return date(y, m, {3: 31, 6: 30, 9: 30, 12: 31}[m])

    def contains(self, d: date) -> bool:
        return self.start <= d <= self.end

    @property
    def due_date(self) -> date:
        m, d = DUE_DATES[self.q]
        return date(self.fy_start + 1 if self.q in (3, 4) else self.fy_start, m, d)

    def previous(self) -> "Quarter":
        return Quarter(self.fy_start, self.q - 1) if self.q > 1 else Quarter(self.fy_start - 1, 4)

    def folder_label(self, style: str = "calendar") -> str:
        if style == "calendar":  # Apr-Jun 2026 -> "2026-Q2" (existing convention)
            y, m = self.months[0]
            return f"{y}-Q{(m - 1) // 3 + 1}"
        return f"{self.fy}-Q{self.q}"

    @property
    def stem(self) -> str:
        return f"144RQ{self.q}"

    @property
    def zip_name(self) -> str:
        return f"{self.fy_start}_{self.fy_start + 1}_{self.stem}.zip"


def expand(p: str) -> Path:
    return Path(os.path.expanduser(p))


class Config:
    def __init__(self, path: Path, data: dict):
        self.path = path
        self.data = data
        self._validate()

    @classmethod
    def load(cls, path: str | None = None) -> "Config":
        p = expand(path or os.environ.get("TDS_CONFIG", DEFAULT_CONFIG))
        if not p.exists():
            raise TdsError(f"Config not found at {p}. Run `tds init` or copy tds_config.example.json there.")
        with p.open() as f:
            return cls(p, json.load(f))

    def save(self) -> None:
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, indent=2) + "\n")
        tmp.replace(self.path)

    def _validate(self) -> None:
        d = self.data
        for k in ("filing_root", "utility", "deductor", "deductee"):
            if k not in d:
                raise TdsError(f"Config missing '{k}'")
        pan = re.compile(r"[A-Z]{5}\d{4}[A-Z]")
        if not re.fullmatch(r"[A-Z]{4}\d{5}[A-Z]", d["deductor"]["tan"]):
            raise TdsError("deductor.tan is not a valid TAN")
        for who in ("deductor", "deductee"):
            if not pan.fullmatch(d[who]["pan"]):
                raise TdsError(f"{who}.pan is not a valid PAN")
        if len(d["deductor"]["address"]) != 5:
            raise TdsError("deductor.address must have 5 lines (flat, building, road, area, district)")

    @property
    def filing_root(self) -> Path:
        return expand(self.data["filing_root"])

    def quarter_dir(self, qt: Quarter) -> Path:
        return self.filing_root / qt.folder_label(self.data.get("folder_label", "calendar"))

    @property
    def fvu_dir(self) -> Path:
        return expand(self.data["utility"]["fvu_dir"])

    @property
    def spec_xlsx(self) -> Path:
        return expand(self.data["utility"]["spec_xlsx"])

    @property
    def version(self) -> str:
        return self.data["utility"]["version"]

    @property
    def deductor(self) -> dict:
        return self.data["deductor"]

    @property
    def deductee(self) -> dict:
        return self.data["deductee"]

    def rent_for(self, y: int, m: int) -> int:
        ov = self.deductee.get("rent_overrides", {})
        return int(ov.get(f"{y}-{m:02d}", self.deductee["monthly_rent"]))

    def rrr(self, qt: Quarter) -> str | None:
        return self.data.get("rrr", {}).get(qt.key)

    def set_rrr(self, qt: Quarter, rrr: str) -> None:
        self.data.setdefault("rrr", {})[qt.key] = rrr
        self.save()
