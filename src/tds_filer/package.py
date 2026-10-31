"""Create the upload zip (contains only the .fvu, as the portal expects)."""

from __future__ import annotations

import zipfile
from pathlib import Path

from .config import Quarter, TdsError


def make_zip(qdir: Path, qt: Quarter) -> Path:
    fvu = qdir / f"{qt.stem}.fvu"
    if not fvu.exists():
        raise TdsError(f"{fvu.name} not found; run `tds validate` first")
    z = qdir / qt.zip_name
    with zipfile.ZipFile(z, "w", zipfile.ZIP_DEFLATED) as zf:
        zf.write(fvu, fvu.name)
    with zipfile.ZipFile(z) as zf:
        names = zf.namelist()
        if names != [fvu.name] or zf.testzip() is not None or zf.read(fvu.name) != fvu.read_bytes():
            raise TdsError(f"{z.name} failed integrity check ({names})")
    return z
