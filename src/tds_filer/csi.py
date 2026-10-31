"""Challan Status Inquiry (.csi) file handling."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from .config import TdsError


@dataclass
class Csi:
    path: Path
    tan: str
    name: str
    versions: list[str]  # FVU versions the file is stamped for, e.g. ["9.5", "2.191", "1.2"]


def find(qdir: Path) -> Path:
    files = sorted(qdir.glob("*.csi"), key=lambda p: p.stat().st_mtime)
    if not files:
        raise TdsError(
            f"No .csi file in {qdir}. Download it from incometax.gov.in "
            "(e-File > e-Pay Tax > Payment History > Download CSI) covering the quarter's challans."
        )
    return files[-1]


def parse(path: Path) -> Csi:
    first = path.read_text(errors="replace").splitlines()[0].split("^")
    if len(first) < 6:
        raise TdsError(f"{path.name}: unexpected CSI header {first!r}")
    return Csi(path, first[1], first[2], first[3:-1])
