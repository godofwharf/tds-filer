"""Extract challan details from Income Tax 'Challan Receipt' PDFs (macOS PDFKit via osascript)."""

from __future__ import annotations

import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from datetime import date, datetime
from pathlib import Path

from .config import Quarter, TdsError

JXA = (
    "function run(argv){ObjC.import('PDFKit');"
    "var d=$.PDFDocument.alloc.initWithURL($.NSURL.fileURLWithPath(argv[0]));"
    "return d.isNil()?'':d.string.js;}"
)


@dataclass
class Challan:
    file: str
    cin: str
    tan: str
    bsr: str
    challan_no: str
    date: str  # DDMMYYYY
    tax: int
    surcharge: int
    cess: int
    interest: int
    penalty: int
    fee: int
    others: int
    total: int
    minor_head: str
    section_code: str

    @property
    def dt(self) -> date:
        return datetime.strptime(self.date, "%d%m%Y").date()


def pdf_text(path: Path) -> str:
    try:
        out = subprocess.run(
            ["osascript", "-l", "JavaScript", "-e", JXA, str(path.resolve())],
            capture_output=True, text=True, timeout=60, check=True,
        ).stdout
    except (FileNotFoundError, subprocess.CalledProcessError) as e:
        raise TdsError(f"Could not read {path.name} (needs macOS PDFKit): {e}") from e
    if not out.strip():
        raise TdsError(f"{path.name}: no text extracted (scanned PDF?)")
    return out


def _amt(s: str) -> int:
    return int(s.replace(",", "").strip())


def _get(pat: str, text: str, name: str, fname: str) -> str:
    m = re.search(pat, text, re.M)
    if not m:
        raise TdsError(f"{fname}: could not find {name} in challan receipt")
    return m.group(1).strip()


def parse_challan(text: str, fname: str = "<challan>") -> Challan:
    g = lambda pat, name: _get(pat, text, name, fname)  # noqa: E731
    dep = g(r"Date of Deposit\s*:\s*(\d{2}-[A-Za-z]{3}-\d{4})", "Date of Deposit")
    breakup = lambda label: _amt(g(rf"^[A-G]\s+{label}\s+₹\s*([\d,]+)", label))  # noqa: E731
    c = Challan(
        file=fname,
        cin=g(r"CIN\s*:\s*(\S+)", "CIN"),
        tan=g(r"TAN\s*:\s*([A-Z]{4}\d{5}[A-Z])", "TAN"),
        bsr=g(r"BSR code\s*:\s*(\d{7})", "BSR code"),
        challan_no=g(r"Challan No\s*:\s*(\d{5})", "Challan No"),
        date=datetime.strptime(dep, "%d-%b-%Y").strftime("%d%m%Y"),
        tax=breakup("Tax"),
        surcharge=breakup("Surcharge"),
        cess=breakup("Education Cess"),
        interest=breakup("Interest"),
        penalty=breakup("Penalty"),
        fee=_amt(g(r"^F\s+Fee[^₹]*₹\s*([\d,]+)", "Fee")),
        others=breakup("Others"),
        total=_amt(g(r"Total \(A\+B\+C\+D\+E\+F\+G\)\s+₹\s*([\d,]+)", "Total")),
        minor_head=g(r"Minor Head\s*:.*\((\d{3})\)", "Minor Head"),
        section_code=g(r"^(\d{4})\s+₹", "Section code"),
    )
    parts = c.tax + c.surcharge + c.cess + c.interest + c.penalty + c.fee + c.others
    if parts != c.total:
        raise TdsError(f"{fname}: breakup {parts} != total {c.total}")
    amt = _amt(g(r"Amount \(in Rs\.\)\s*:\s*₹\s*([\d,]+)", "Amount"))
    if amt != c.total:
        raise TdsError(f"{fname}: header amount {amt} != total {c.total}")
    return c


def collect(root: Path, qdir: Path, qt: Quarter) -> list[Challan]:
    """Move in-quarter challan PDFs from the filing root into qdir, then parse all in qdir."""
    qdir.mkdir(parents=True, exist_ok=True)
    for p in sorted(root.glob("*ChallanReceipt*.pdf")):
        c = parse_challan(pdf_text(p), p.name)
        if qt.contains(c.dt):
            shutil.move(str(p), qdir / p.name)
            print(f"  moved {p.name} -> {qdir.name}/")
    out = []
    for p in sorted(qdir.glob("*ChallanReceipt*.pdf")):
        c = parse_challan(pdf_text(p), p.name)
        if not qt.contains(c.dt):
            raise TdsError(f"{p.name} dated {c.dt} is outside {qt.key}; move it to the right folder")
        out.append(c)
    out.sort(key=lambda c: c.dt)
    return out


def save(challans: list[Challan], qdir: Path) -> None:
    (qdir / "challans.json").write_text(json.dumps([asdict(c) for c in challans], indent=2) + "\n")
    lines = []
    for c in challans:
        lines += [
            c.dt.strftime("%b"), f"BSR - {c.bsr}", f"Challan No - {c.challan_no}",
            f"Date of Payment - {c.dt:%d/%m/%Y}", f"Amount - {c.total}", f"Tax - {c.tax}",
            f"Cess - {c.cess}", "",
        ]
    (qdir / "Challan Details").write_text("\n".join(lines).rstrip() + "\n")


def load(qdir: Path) -> list[Challan]:
    p = qdir / "challans.json"
    if not p.exists():
        raise TdsError(f"{p} missing; run `tds extract` first")
    return [Challan(**d) for d in json.loads(p.read_text())]
