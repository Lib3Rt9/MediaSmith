"""Command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional

from . import __version__
from .config import ConfigError, load_config
from .core import (ALREADY_DONE, INPUT_EXTS, SIZE_STATUSES, SKIPPED, Context, FileResult,
                   Job, Options, PathReserver, parse_size, run_jobs)
from .identity import COMMAND_NAME
from .report import describe, failure_lines, summarize, write_log, write_report
from .resume import MANIFEST_NAME, Manifest

try:
    from tqdm import tqdm
except ImportError:  # progress bar is optional
    tqdm = None

DEFAULT_LOG_NAME = f"{COMMAND_NAME}.log"
DEFAULT_MAX_EDGE = 2560
SIZE_KEYS = ("max_edge", "width", "height", "scale")
CONFIG_ALIASES = {"copy_jpg": "copy_same", "to": "format"}


class UsageError(Exception):
    pass


def build_parser(suppress: bool = False) -> argparse.ArgumentParser:
    """suppress=True gives a parser whose result only contains options typed on the command
    line (used to tell CLI values apart from config-file values)."""
    kw = {"argument_default": argparse.SUPPRESS} if suppress else {}
    p = argparse.ArgumentParser(
        prog=COMMAND_NAME,
        description="Shrink and convert folders of images for archiving.", **kw)
    p.add_argument("input_folder", type=Path)
    p.add_argument("output_folder", type=Path)

    size = p.add_argument_group("resizing (choose one; default --max-edge 2560)")
    excl = size.add_mutually_exclusive_group()
    # No explicit default= here on purpose: argparse gives None, and the "suppress" parser
    # then leaves these out unless they were typed on the command line.
    excl.add_argument("--max-edge", type=int, metavar="PX",
                      help="Longer side becomes at most PX pixels")
    excl.add_argument("--width", type=int, metavar="PX",
                      help="Width becomes at most PX pixels")
    excl.add_argument("--height", type=int, metavar="PX",
                      help="Height becomes at most PX pixels")
    excl.add_argument("--scale", type=float, metavar="PERCENT",
                      help="Scale both sides by PERCENT (e.g. 50 = half size)")
    size.add_argument("--upscale", action="store_true",
                      help="Allow enlarging images (by default nothing is ever upscaled)")

    out = p.add_argument_group("output")
    out.add_argument("--format", choices=["jpg", "png", "webp", "avif"], default="jpg",
                     help="Output format (default jpg)")
    out.add_argument("--quality", type=int, default=95,
                     help="Quality 1-100 for jpg/webp/avif (default 95)")
    out.add_argument("--lossless", action="store_true", help="Lossless WebP")
    out.add_argument("--target-size", default=None, metavar="SIZE",
                     help="Aim for files of at most SIZE (e.g. 500KB, 2MB) by lowering quality")
    out.add_argument("--min-quality", type=int, default=30,
                     help="Lowest quality --target-size may use (default 30)")
    out.add_argument("--strip-metadata", action="store_true",
                     help="Remove EXIF (rotation is applied first); ICC colour profile is kept")
    out.add_argument("--strip-icc", action="store_true", help="Also remove the ICC colour profile")
    out.add_argument("--rename", default=None, metavar="TEMPLATE",
                     help="Rename outputs, e.g. '{parent}_{n:04d}'. Fields: {name} {n} {date} "
                          "{parent} {width} {height} {orig_ext} {format}")

    beh = p.add_argument_group("behaviour")
    beh.add_argument("--recursive", action="store_true",
                     help="Also process sub-folders (structure is kept)")
    beh.add_argument("--copy-same", "--copy-jpg", dest="copy_same", action="store_true",
                     help="Copy files already in the output format untouched, "
                          "even if larger than the size limit (--copy-jpg for JPG)")
    beh.add_argument("--dry-run", action="store_true",
                     help="Show what would happen; write nothing")
    beh.add_argument("--resume", action="store_true",
                     help="Skip files already processed earlier with the same options")
    beh.add_argument("--jobs", type=int, default=1, metavar="N",
                     help="Worker threads (default 1; 0 = all CPU cores)")

    rep = p.add_argument_group("logs and reports")
    rep.add_argument("--log", type=Path, default=None, metavar="FILE",
                     help=f"Log with summary and every failure + reason "
                          f"(default: {DEFAULT_LOG_NAME} in the output folder)")
    rep.add_argument("--report", type=Path, default=None, metavar="FILE",
                     help="Write a per-file report; .json or .csv")
    rep.add_argument("--no-progress", action="store_true", help="No progress bar / counter")
    rep.add_argument("--quiet", action="store_true", help="Only print the summary and failures")

    p.add_argument("--config", type=Path, default=None, metavar="FILE",
                   help="Read default options from a TOML or JSON file")
    p.add_argument("--version", action="version", version=f"{COMMAND_NAME} {__version__}")
    return p


def _config_defaults(parser: argparse.ArgumentParser, path: Path) -> dict:
    try:
        cfg = load_config(path)
    except ConfigError as e:
        raise UsageError(str(e))
    cfg = {CONFIG_ALIASES.get(k, k): v for k, v in cfg.items()}
    valid = {a.dest for a in parser._actions if a.option_strings} - {"help", "version", "config"}
    unknown = sorted(set(cfg) - valid)
    if unknown:
        raise UsageError(f"Unknown option(s) in {path}: {', '.join(unknown)}")
    return cfg


def build_options(args: argparse.Namespace, explicit: set) -> Options:
    given = [k for k in SIZE_KEYS if k in explicit]
    if given:
        key = given[0]
    else:
        from_config = [k for k in SIZE_KEYS if getattr(args, k) is not None]
        if len(from_config) > 1:
            raise UsageError("The config file sets more than one of "
                             "max-edge / width / height / scale; keep only one.")
        key = from_config[0] if from_config else "max_edge"
    value = getattr(args, key)
    if value is None:
        value = DEFAULT_MAX_EDGE

    try:
        target = parse_size(args.target_size) if args.target_size is not None else None
        opts = Options(
            fmt=args.format, resize_mode=key, resize_value=value, upscale=args.upscale,
            quality=args.quality, min_quality=args.min_quality, lossless=args.lossless,
            target_bytes=target, strip_metadata=args.strip_metadata, strip_icc=args.strip_icc,
            copy_same=args.copy_same, rename=args.rename, dry_run=args.dry_run)
        opts.validate()
    except ValueError as e:
        raise UsageError(str(e))
    return opts


def main(argv: Optional[List[str]] = None) -> int:
    try:
        return _main(argv)
    except UsageError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


def _main(argv: Optional[List[str]]) -> int:
    parser = build_parser()

    pre = argparse.ArgumentParser(add_help=False)
    pre.add_argument("--config", type=Path, default=None)
    known, _ = pre.parse_known_args(argv)
    if known.config:
        parser.set_defaults(**_config_defaults(parser, known.config))

    args = parser.parse_args(argv)
    explicit = set(vars(build_parser(suppress=True).parse_args(argv)))
    opts = build_options(args, explicit)

    if args.jobs < 0:
        raise UsageError("--jobs must be 0 or more.")
    workers = (os.cpu_count() or 1) if args.jobs == 0 else args.jobs

    src_dir = args.input_folder.resolve()
    dst_dir = args.output_folder.resolve()
    if not src_dir.is_dir():
        raise UsageError(f"Input folder not found: {src_dir}")
    if src_dir == dst_dir:
        raise UsageError("Output folder must be different from the input folder.")

    log_path = (args.log or (dst_dir / DEFAULT_LOG_NAME)).resolve()
    report_path = args.report.resolve() if args.report else None
    if report_path and report_path.suffix.lower() not in (".json", ".csv"):
        raise UsageError("--report must end in .json or .csv")

    if not opts.dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)

    excluded = {p for p in (log_path, report_path) if p}
    candidates = src_dir.rglob("*") if args.recursive else src_dir.iterdir()
    files = sorted(f for f in candidates
                   if f.is_file() and dst_dir not in f.parents and f.resolve() not in excluded)

    fingerprint = opts.fingerprint()
    manifest = Manifest(dst_dir / MANIFEST_NAME)
    if args.resume:
        manifest.load()

    started = datetime.now()
    results: List[FileResult] = []
    todo: List[Job] = []
    stats: Dict[int, tuple] = {}  # order -> (size, mtime_ns), for the manifest
    number = 0
    for order, f in enumerate(files):
        rel = f.relative_to(src_dir)
        if f.suffix.lower() not in INPUT_EXTS:
            results.append(FileResult(status=SKIPPED, src=f, rel=rel, order=order))
            continue
        number += 1
        st = f.stat()
        stats[order] = (st.st_size, st.st_mtime_ns)
        entry = manifest.lookup(rel, st.st_size, st.st_mtime_ns, fingerprint, dst_dir) \
            if args.resume else None
        if entry:
            results.append(FileResult(
                status=ALREADY_DONE, src=f, rel=rel, order=order, dst=dst_dir / entry["dst"],
                bytes_in=entry["bytes_in"], bytes_out=entry["bytes_out"]))
        else:
            todo.append(Job(order=order, src=f, rel=rel, number=number))

    if manifest.stale:
        print(f"Note: {manifest.stale} file(s) were processed earlier with different options; "
              f"they will be processed again (new names get a _1 suffix). "
              f"Use a fresh output folder to avoid duplicates.", file=sys.stderr)
    if opts.dry_run:
        print("DRY RUN - nothing will be written.\n")

    show_progress = not args.no_progress and not args.quiet
    bar = tqdm(total=len(todo), unit="file") if (
        show_progress and tqdm is not None and sys.stderr.isatty()) else None
    counter = {"done": 0}

    def emit(msg: str) -> None:
        if bar is not None:
            tqdm.write(msg)
        else:
            print(msg)

    def on_result(res: FileResult) -> None:
        counter["done"] += 1
        results.append(res)
        if res.status in SIZE_STATUSES and not opts.dry_run:
            size, mtime_ns = stats[res.order]
            manifest.record(res, size, mtime_ns, fingerprint, dst_dir)
        if not args.quiet:
            prefix = f"[{counter['done']}/{len(todo)}] " if show_progress and bar is None else ""
            emit(prefix + describe(res))
        if bar is not None:
            bar.update(1)

    ctx = Context(dst_dir=dst_dir, opts=opts, reserver=PathReserver())
    interrupted = run_jobs(todo, ctx, workers, on_result)
    if bar is not None:
        bar.close()
    if interrupted:
        print("\nInterrupted - writing what was done so far.")

    results.sort(key=lambda r: r.order)

    print("\n--- Done ---" if not opts.dry_run else "\n--- Dry run result ---")
    for line in summarize(results, opts.dry_run):
        print(line)

    failures = failure_lines(results)
    if failures:
        print(f"\n--- Failed files ({len(failures)}) ---")
        for line in failures:
            print(line)

    if not opts.dry_run:
        for label, path, writer in (("Log", log_path, write_log),
                                    ("Report", report_path, write_report)):
            if path is None:
                continue
            try:
                writer(path, src_dir, dst_dir, opts, args.recursive, workers, results,
                       started, interrupted)
                print(f"\n{label} written to: {path}")
            except OSError as e:
                print(f"\nCould not write {label.lower()} file {path}: {e}")

    if interrupted:
        return 130
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
