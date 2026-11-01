"""Run Protean's standalone FVU headlessly and parse its outputs."""

from __future__ import annotations

import hashlib
import html
import re
import shutil
import subprocess
import time
from pathlib import Path

from .config import Config, Quarter, TdsError

OUTPUT_SUFFIXES = (".fvu", ".err", ".html", "err.html", ".fvu.log", ".fvu.uenc")


def outputs(qdir: Path, qt: Quarter) -> list[Path]:
    s = qt.stem
    names = [f"{s}{x}" for x in OUTPUT_SUFFIXES] + ["rp.png", qt.zip_name]
    found = [qdir / n for n in names if (qdir / n).exists()]
    return found + sorted(qdir.glob(f"*_144_Q{qt.q}_*.pdf"))


def archive_previous(qdir: Path, qt: Quarter) -> Path | None:
    files = outputs(qdir, qt)
    if not files:
        return None
    dest = qdir / "old_run" / time.strftime("%Y%m%d-%H%M%S")
    dest.mkdir(parents=True)
    for f in files:
        shutil.move(str(f), dest / f.name)
    return dest


def _sha256(p: Path) -> str:
    h = hashlib.sha256()
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify_integrity(cfg: Config) -> None:
    """Everything on FVU's classpath runs as you: refuse if others can modify it, or if pinned hashes differ."""
    jar_dir = cfg.fvu_dir
    targets = [jar_dir, *jar_dir.glob("*.jar"), jar_dir / "e-mudhra.cer"]
    loose = [p.name for p in targets if p.exists() and p.stat().st_mode & 0o022]
    if loose:
        raise TdsError(f"FVU files writable by group/others: {loose}. Fix: chmod -R go-w '{jar_dir}'")
    pins: dict = cfg.data["utility"].get("sha256", {})
    for name, want in pins.items():
        p = jar_dir / name
        if not p.exists() or _sha256(p) != want.lower():
            raise TdsError(f"{name} does not match pinned sha256 in config; re-download FVU from Protean")
    if not pins:
        print("  note: no utility.sha256 pins in config; run `tds init` to record them")


def pin_hashes(cfg: Config) -> dict[str, str]:
    return {p.name: _sha256(p) for p in sorted(cfg.fvu_dir.glob("*.jar"))}


def run(cfg: Config, qt: Quarter, qdir: Path, csi_path: Path, timeout: int = 600) -> None:
    jar_dir = cfg.fvu_dir
    jars = list(jar_dir.glob("TDS_STANDALONE_FVU_*.jar"))
    if not jars:
        raise TdsError(f"No TDS_STANDALONE_FVU_*.jar in {jar_dir}. Download FVU {cfg.version} from Protean.")
    if not (jar_dir / "e-mudhra.cer").exists():
        raise TdsError(f"{jar_dir}/e-mudhra.cer missing; re-extract the FVU zip")
    if shutil.which("java") is None:
        raise TdsError("java not found on PATH")
    verify_integrity(cfg)
    s = qdir / qt.stem
    # FVU CLI: <input.txt> <error.err> <output.fvu> 0 <version> 0 <challan.csi>
    # Must run with cwd = FVU dir (loads e-mudhra.cer relatively). Not java.awt.headless: it builds a JFrame.
    cmd = ["java", "-cp", "./*", "com.tin.FVU.FVU", f"{s}.txt", f"{s}.err", f"{s}.fvu",
           "0", cfg.version, "0", str(csi_path)]
    print(f"  running FVU {cfg.version} (may take ~1-2 min; it checks Protean's version service)...")
    # FVU keeps the JVM alive after finishing (result dialog / Desktop.browse), so poll for outputs
    # and terminate once they are complete and stable.
    done_files = [Path(f"{s}.fvu"), Path(f"{s}.html")]
    proc = subprocess.Popen(cmd, cwd=jar_dir, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.monotonic() + timeout
    last_sizes = None
    try:
        while time.monotonic() < deadline:
            if proc.poll() is not None:
                break
            err = Path(f"{s}.err")
            ready = err.exists() or (all(p.exists() for p in done_files) and any(qdir.glob(f"*_144_Q{qt.q}_*.pdf")))
            if ready:
                sizes = [p.stat().st_size for p in qdir.iterdir() if p.is_file()]
                if sizes == last_sizes:
                    break
                last_sizes = sizes
            time.sleep(3)
        else:
            raise TdsError(f"FVU timed out after {timeout}s; check {jar_dir}/fvu.log")
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(10)
            except subprocess.TimeoutExpired:
                proc.kill()
    log = qdir / f"{qt.stem}.fvu.log"
    if not (qdir / f"{qt.stem}.fvu").exists() and not (qdir / f"{qt.stem}.err").exists():
        tail = log.read_text(errors="replace")[-1500:] if log.exists() else (jar_dir / "fvu.log").read_text(errors="replace")[-1500:]
        raise TdsError(f"FVU produced neither .fvu nor .err. Log tail:\n{tail}")


def format_errors(err: Path) -> str:
    rows = []
    for line in err.read_text(errors="replace").splitlines():
        f = line.split("^")
        if len(f) >= 7:
            rows.append(f"    line {f[0]} {f[2]} (challan {f[3] or '-'}, deductee {f[4] or '-'}): {f[5]} {f[6].strip()}")
        elif line.strip():
            rows.append("    " + line.strip())
    return "\n".join(rows)


def stats(path: Path) -> dict | None:
    if not path.exists():
        return None
    t = " ".join(html.unescape(re.sub(r"<[^>]+>", " ", path.read_text(errors="replace"))).split())
    m = re.search(r"Form Type (\S+) (\d+) ", t)
    h = re.search(r"Type of Correction (\d{4}-\d{2}) (\d{4}-\d{2}) (Q\d)", t)
    c = re.search(r"Higher Rate (\d+) ([\d.]+) (\d+) (\d+)", t)
    u = re.search(r"No\. of Matched challan (\d+) (\d+) (\d+)", t)
    p = re.search(r"\(PANINVALID\) (\d+) (\d+) (\d+) (\d+)", t)
    if not (m and h and c and u and p):
        return None
    return {
        "form": m.group(2), "ay": h.group(1), "tax_year": h.group(2), "quarter": h.group(3),
        "challans": int(c.group(1)), "total": float(c.group(2)), "deductees": int(c.group(3)),
        "higher_rate": int(c.group(4)), "unmatched": int(u.group(2)), "matched": int(u.group(3)),
        "valid_pan": int(p.group(1)), "pan_applied": int(p.group(2)),
        "pan_not_available": int(p.group(3)), "invalid_pan": int(p.group(4)),
    }
