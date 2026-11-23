"""Console summary, log file and JSON/CSV report."""

from __future__ import annotations

import csv
import json
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from typing import List

from . import __version__
from .core import (ALREADY_DONE, CONVERTED, COPIED, ERROR, KEPT_ORIGINAL, SIZE_STATUSES,
                   SKIPPED, FileResult, Options)
from .identity import COMMAND_NAME, PROJECT_NAME

CSV_COLUMNS = ["status", "source", "relative_path", "destination", "bytes_in", "bytes_out",
               "old_width", "old_height", "new_width", "new_height", "quality",
               "target_met", "error"]


def describe(r: FileResult) -> str:
    """One console line per file."""
    if r.status == COPIED:
        renamed = f" -> {r.dst.name}" if r.dst and r.dst.name != r.src.name else ""
        return f"[COPY]    {r.rel}{renamed}"
    if r.status == KEPT_ORIGINAL:
        renamed = f" -> {r.dst.name}" if r.dst and r.dst.name != r.src.name else ""
        return (f"[KEEP]    {r.rel}{renamed}  new version would be bigger "
                f"({r.bytes_in / 1024:.0f} KB -> {r.candidate_bytes / 1024:.0f} KB), original copied")
    if r.status == CONVERTED:
        o, n = r.old_size, r.new_size
        extra = ""
        if r.target_met is not None:
            extra = f"  q={r.quality}" + ("" if r.target_met else " (target size not reached)")
        return (f"[CONVERT] {r.rel} -> {r.dst.name}  {o[0]}x{o[1]} -> {n[0]}x{n[1]}  "
                f"({r.bytes_in / 1024:.0f} KB -> {r.bytes_out / 1024:.0f} KB){extra}")
    if r.status == ALREADY_DONE:
        return f"[DONE]    {r.rel}  already processed"
    if r.status == ERROR:
        return f"[ERROR]   {r.rel}: {r.error}"
    return f"[SKIP]    {r.rel}"


def _count(results: List[FileResult], status: str) -> int:
    return sum(1 for r in results if r.status == status)


def summary_dict(results: List[FileResult]) -> dict:
    converted = [r for r in results if r.status == CONVERTED]
    ok = [r for r in results if r.status in SIZE_STATUSES]
    return {
        "converted": len(converted),
        "resized": sum(1 for r in converted if r.new_size != r.old_size),
        "copied_untouched": _count(results, COPIED),
        "kept_original": _count(results, KEPT_ORIGINAL),
        "already_done": _count(results, ALREADY_DONE),
        "skipped": _count(results, SKIPPED),
        "failed": _count(results, ERROR),
        "bytes_in": sum(r.bytes_in for r in ok),
        "bytes_out": sum(r.bytes_out for r in ok),
    }


def summarize(results: List[FileResult], dry_run: bool = False) -> List[str]:
    s = summary_dict(results)
    verb = "Would convert" if dry_run else "Converted/re-saved"
    lines = [
        f"{verb}: {s['converted']}  (of which resized: {s['resized']})",
        f"Copied untouched (already in target format): {s['copied_untouched']}",
        f"Original kept because the new file would be bigger: {s['kept_original']}",
    ]
    if s["already_done"]:
        lines.append(f"Already done (skipped by --resume): {s['already_done']}")
    lines += [
        f"Skipped (non-image files): {s['skipped']}",
        f"Failed: {s['failed']}",
        f"Total size: {s['bytes_in'] / 1048576:.1f} MB -> {s['bytes_out'] / 1048576:.1f} MB "
        f"(saved {(s['bytes_in'] - s['bytes_out']) / 1048576:.1f} MB)",
    ]
    return lines


def failure_lines(results: List[FileResult]) -> List[str]:
    return [f"  {r.rel}\n      reason: {r.error}" for r in results if r.status == ERROR]


def record_dict(r: FileResult) -> dict:
    return {
        "status": r.status,
        "source": str(r.src),
        "relative_path": r.rel.as_posix(),
        "destination": str(r.dst) if r.dst else "",
        "bytes_in": r.bytes_in,
        "bytes_out": r.bytes_out,
        "old_width": r.old_size[0] if r.old_size else "",
        "old_height": r.old_size[1] if r.old_size else "",
        "new_width": r.new_size[0] if r.new_size else "",
        "new_height": r.new_size[1] if r.new_size else "",
        "quality": r.quality if r.quality is not None else "",
        "target_met": "" if r.target_met is None else r.target_met,
        "error": r.error or "",
    }


def describe_options(opts: Options, recursive: bool, jobs: int) -> str:
    d = asdict(opts)
    d["recursive"] = recursive
    d["jobs"] = jobs
    return " ".join(f"{k}={v}" for k, v in d.items() if v not in (None, False))


def write_log(log_path: Path, src_dir: Path, dst_dir: Path, opts: Options, recursive: bool,
              jobs: int, results: List[FileResult], started: datetime,
              interrupted: bool) -> None:
    """Summary plus every failure with its reason."""
    log_path.parent.mkdir(parents=True, exist_ok=True)
    failures = failure_lines(results)
    lines = [
        f"{PROJECT_NAME} {__version__} log",
        f"Started : {started:%Y-%m-%d %H:%M:%S}",
        f"Finished: {datetime.now():%Y-%m-%d %H:%M:%S}",
        f"Input   : {src_dir}",
        f"Output  : {dst_dir}",
        f"Options : {describe_options(opts, recursive, jobs)}",
    ]
    if interrupted:
        lines.append("NOTE: run was interrupted (Ctrl-C); the list below is incomplete.")
    lines += ["", "--- Summary ---", *summarize(results), ""]
    if failures:
        lines.append(f"--- Failed files ({len(failures)}) ---")
        lines += failures
    else:
        lines.append("No failures.")
    log_path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def write_report(path: Path, src_dir: Path, dst_dir: Path, opts: Options, recursive: bool,
                 jobs: int, results: List[FileResult], started: datetime,
                 interrupted: bool) -> None:
    """JSON (summary + options + every file) or CSV (every file), chosen by extension."""
    path.parent.mkdir(parents=True, exist_ok=True)
    records = [record_dict(r) for r in results if r.status != SKIPPED]
    if path.suffix.lower() == ".csv":
        with path.open("w", newline="", encoding="utf-8") as fh:
            writer = csv.DictWriter(fh, fieldnames=CSV_COLUMNS)
            writer.writeheader()
            writer.writerows(records)
        return
    doc = {
        "tool": COMMAND_NAME,
        "version": __version__,
        "started": started.isoformat(timespec="seconds"),
        "finished": datetime.now().isoformat(timespec="seconds"),
        "interrupted": interrupted,
        "input": str(src_dir),
        "output": str(dst_dir),
        "options": {**asdict(opts), "recursive": recursive, "jobs": jobs},
        "summary": summary_dict(results),
        "files": records,
    }
    path.write_text(json.dumps(doc, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
