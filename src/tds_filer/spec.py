"""Parse Protean's Form 144 file-format spreadsheet (xlsx) using only the stdlib."""

from __future__ import annotations

import html
import re
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from .config import TdsError

SECTIONS = {
    "File Header Record": "FH",
    "Batch Header Record": "BH",
    "Challan Detail Record": "CD",
    "Deductee Detail Record": "DD",
}


@dataclass
class FieldSpec:
    n: int
    name: str
    dtype: str
    size: int | None
    mandatory: bool
    must_be_empty: bool
    remarks: str


@dataclass
class Spec:
    records: dict[str, dict[int, FieldSpec]] = field(default_factory=dict)
    remittance: dict[str, set[str]] = field(default_factory=dict)  # section code -> allowed codes
    countries: dict[str, str] = field(default_factory=dict)  # code -> name
    states: dict[str, str] = field(default_factory=dict)

    def count(self, rec: str) -> int:
        return max(self.records[rec])


def _rows(path: Path) -> list[list[str]]:
    try:
        z = zipfile.ZipFile(path)
    except (FileNotFoundError, zipfile.BadZipFile) as e:
        raise TdsError(f"Cannot open format spec {path}: {e}") from e
    ss = [
        html.unescape(re.sub(r"<[^>]+>", "", s))
        for s in re.findall(r"<si>(.*?)</si>", z.read("xl/sharedStrings.xml").decode(), re.S)
    ]
    rows = []
    for r in re.findall(r"<row[^>]*>(.*?)</row>", z.read("xl/worksheets/sheet1.xml").decode(), re.S):
        cells = []
        for attrs, body in re.findall(r"<c ([^>]*?)(?:/>|>(.*?)</c>)", r, re.S):
            m = re.search(r"<v>(.*?)</v>", body or "")
            if m:
                v = ss[int(m.group(1))] if 't="s"' in attrs else m.group(1)
                cells.append(" ".join(v.split()))
        rows.append(cells)
    return rows


def _field(c: list[str]) -> FieldSpec:
    rest = c[2:]
    dtypes = {"CHAR", "INTEGER", "DECIMAL", "DATE", "NA"}
    dtype = next((x for x in rest if x in dtypes), "")
    size = None
    if dtype:
        after = rest[rest.index(dtype) + 1:]
        if after and after[0].isdigit():
            size = int(after[0])
    remarks = rest[-1] if rest else ""
    flags = rest[:5]
    return FieldSpec(
        n=int(c[0]), name=c[1], dtype=dtype, size=size,
        mandatory="M" in flags,
        must_be_empty=bool(re.search(r"No value (should|to) be (specified|provided)\.?\s*$", remarks))
        and "M" not in flags,
        remarks=remarks,
    )


def load(path: Path) -> Spec:
    spec = Spec()
    sec = None
    mode = None
    cur_section = None
    for c in _rows(path):
        if not c:
            continue
        head = c[0]
        hit = next((v for k, v in SECTIONS.items() if k in head), None)
        if hit:
            sec, mode = hit, "fields"
            spec.records[sec] = {}
            continue
        if head.startswith("Nature of Remittance"):
            mode = "remit"
            continue
        if head.startswith("Annexure 10"):
            mode = "country"
            continue
        if head.startswith("Annexure - 1") or head.startswith("Annexure 1 "):
            mode = "state"
            continue
        if head.startswith("Annexure"):
            mode = None
            continue
        if mode == "fields" and head.isdigit() and len(c) >= 3:
            spec.records[sec][int(head)] = _field(c)
        elif mode == "fields" and head.startswith(("Note", "Separate", "Single")):
            mode = None
        elif mode == "remit":
            nums = [x for x in c if re.fullmatch(r"\d{2,4}", x)]
            for x in nums:
                if len(x) == 4:
                    cur_section = x
            codes = [x for x in nums if len(x) == 2]
            if cur_section and codes:
                spec.remittance.setdefault(cur_section, set()).add(codes[0])
        elif mode == "country" and len(c) >= 2 and re.fullmatch(r"\d{1,3}", c[-1]):
            spec.countries[c[-1].zfill(2)] = c[0]
        elif mode == "state" and len(c) >= 2 and re.fullmatch(r"\d{2}", c[-1]):
            spec.states[c[-1]] = c[0]
    for rec in SECTIONS.values():
        if not spec.records.get(rec):
            raise TdsError(f"Format spec {path.name}: section {rec} not found")
    return spec
