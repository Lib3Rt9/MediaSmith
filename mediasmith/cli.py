"""Command-line interface."""

from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple

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
    p.add_argument("paths", type=Path, nargs="*", metavar="PATH",
                   help="Input file(s) or folder(s); two paths retain INPUT_FOLDER OUTPUT_FOLDER form")
    p.add_argument("-i", "--input", dest="input_paths", type=Path, nargs="+",
                   action="append", metavar="PATH",
                   help="Add input file(s) or folder(s); repeat -i to add more")
    p.add_argument("--all", dest="all_inputs", action="store_true",
                   help="Also include images and folders in the current directory")
    p.add_argument("--exclude", action="append", type=Path, default=[], metavar="PATH_OR_NAME",
                   help="Exclude exact paths (including folder contents) or matching names; repeat as needed")
    p.add_argument("-o", "--output", dest="output_folder", type=Path, default=None,
                   help="Output directory (default: output)")

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
    beh.add_argument("--convert-only", action="store_true",
                     help="Convert format without resizing; size and quality options still apply")
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


def unique_directory(path: Path) -> Path:
    """Choose a fresh output directory without overwriting a previous run."""
    if not path.exists():
        return path
    index = 1
    while True:
        candidate = path.with_name(f"{path.name}_{index}")
        if not candidate.exists():
            return candidate
        index += 1


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
            copy_same=args.copy_same, convert_only=args.convert_only,
            rename=args.rename, dry_run=args.dry_run)
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

    legacy_pair = (
        len(args.paths) == 2 and not args.input_paths and not args.all_inputs
        and args.output_folder is None
    )
    if legacy_pair:
        requested_inputs = [args.paths[0]]
        output_arg = args.paths[1]
    else:
        requested_inputs = list(args.paths)
        requested_inputs.extend(path for group in (args.input_paths or []) for path in group)
        output_arg = args.output_folder or Path("output")

    if not requested_inputs and not args.all_inputs:
        parser.print_help()
        return 0

    cwd = Path.cwd()
    requested_dst = output_arg.resolve()
    dst_dir = requested_dst if args.resume or legacy_pair else unique_directory(requested_dst)
    if args.all_inputs:
        requested_inputs.extend(
            path for path in sorted(cwd.iterdir())
            if path.resolve() not in (requested_dst, dst_dir)
            and (path.is_dir() or path.is_file() and path.suffix.lower() in INPUT_EXTS)
        )

    exclusions = [
        path.resolve() if path.is_absolute() else (cwd / path).resolve()
        for path in args.exclude
    ]

    def is_excluded(path: Path) -> bool:
        resolved = path.resolve()
        for exclusion, raw in zip(exclusions, args.exclude):
            if resolved == exclusion or exclusion in resolved.parents:
                return True
            if len(raw.parts) == 1 and any(part.name == raw.name for part in (path, *path.parents)):
                return True
        return False

    input_paths: List[Path] = []
    seen_inputs = set()
    skipped_inputs: List[str] = []
    valid_inputs: List[Tuple[Path, bool]] = []
    for path in requested_inputs:
        resolved = path.resolve()
        if resolved in seen_inputs:
            continue
        seen_inputs.add(resolved)
        input_paths.append(resolved)
        if is_excluded(resolved):
            skipped_inputs.append(f"{resolved}: excluded by --exclude")
        elif resolved.is_dir():
            valid_inputs.append((resolved, True))
        elif resolved.is_file() and resolved.suffix.lower() in INPUT_EXTS:
            valid_inputs.append((resolved, False))
        else:
            reason = ("not a supported image file" if resolved.is_file()
                      else "not found or not a file/folder")
            skipped_inputs.append(f"{resolved}: {reason}")
            print(f"Skipping invalid input: {resolved}: {reason}")

    if args.recursive:
        input_directories = [path for path, is_dir in valid_inputs if is_dir]
        uncovered_inputs: List[Tuple[Path, bool]] = []
        for path, is_dir in valid_inputs:
            covering_directory = next(
                (directory for directory in input_directories if directory in path.parents),
                None,
            )
            if covering_directory is None:
                uncovered_inputs.append((path, is_dir))
            else:
                skipped_inputs.append(
                    f"{path}: already covered by input folder {covering_directory}"
                )
        valid_inputs = uncovered_inputs

    if not valid_inputs:
        raise UsageError("No valid input files or folders were selected.")

    for input_path, is_dir in valid_inputs:
        if is_dir and (input_path == dst_dir or input_path in dst_dir.parents):
            raise UsageError("Output folder must not be inside an input folder.")
        if not is_dir and dst_dir in input_path.parents:
            raise UsageError("Output folder must not contain or be the parent of an input file.")

    log_path = (args.log or (dst_dir / DEFAULT_LOG_NAME)).resolve()
    report_path = args.report.resolve() if args.report else None
    if report_path and report_path.suffix.lower() not in (".json", ".csv"):
        raise UsageError("--report must end in .json or .csv")

    if not opts.dry_run:
        dst_dir.mkdir(parents=True, exist_ok=True)

    excluded_files = {p for p in (log_path, report_path, dst_dir / MANIFEST_NAME) if p}
    input_outputs = []
    reserved_roots = set()
    for input_path, is_dir in valid_inputs:
        if legacy_pair or not is_dir:
            input_root = Path()
        else:
            base = Path(input_path.name or "input")
            input_root = base
            index = 1
            while ((dst_dir / input_root).exists() and not args.resume) \
                    or input_root in reserved_roots:
                input_root = base.with_name(f"{base.name}_{index}")
                index += 1
            reserved_roots.add(input_root)
        input_outputs.append((input_path, is_dir, input_root))

    manifest = Manifest(dst_dir / MANIFEST_NAME)
    if args.resume:
        manifest.load()

    direct_files = [path for path, is_dir in valid_inputs if not is_dir]
    try:
        direct_common_parent = (
            Path(os.path.commonpath([str(path.parent.resolve()) for path in direct_files]))
            if direct_files else None
        )
    except ValueError:
        direct_common_parent = None

    started = datetime.now()
    results: List[FileResult] = []
    todo: List[Job] = []
    stats: Dict[int, tuple] = {}  # order -> (size, mtime_ns), for the manifest
    number = 0
    empty_directories = set()
    skipped_directories = set()
    folder_directories = []
    fingerprint = opts.fingerprint()
    order = 0
    for input_path, is_dir, input_root in input_outputs:
        if is_dir:
            candidates = input_path.rglob("*") if args.recursive else input_path.iterdir()
            files = sorted(
                f for f in candidates
                if f.is_file() and dst_dir not in f.parents
                and f.resolve() not in excluded_files and not is_excluded(f)
            )
            directories = [input_path]
            directories.extend(sorted(path for path in input_path.rglob("*") if path.is_dir()))
            folder_directories.append(directories)
        else:
            files = [input_path]

        for f in files:
            if is_dir:
                rel = input_root / f.relative_to(input_path)
                source_key = f"folder:{rel.as_posix()}"
            else:
                rel = Path(f.name)
                if direct_common_parent is not None:
                    try:
                        source_key = f"file:{f.resolve().relative_to(direct_common_parent).as_posix()}"
                    except ValueError:
                        source_key = f"file:{f.parent.name}/{f.name}"
                else:
                    source_key = f"file:{f.parent.name}/{f.name}"
            if f.suffix.lower() not in INPUT_EXTS:
                results.append(FileResult(status=SKIPPED, src=f, rel=rel, order=order))
                order += 1
                continue
            number += 1
            st = f.stat()
            stats[order] = (st.st_size, st.st_mtime_ns)
            entry = manifest.lookup(source_key, rel, st.st_size, st.st_mtime_ns, fingerprint, dst_dir) \
                if args.resume else None
            if entry:
                results.append(FileResult(
                    status=ALREADY_DONE, src=f, rel=rel, order=order, dst=dst_dir / entry["dst"],
                    bytes_in=entry["bytes_in"], bytes_out=entry["bytes_out"],
                    source_key=source_key))
            else:
                todo.append(Job(order=order, src=f, rel=rel, number=number,
                                source_key=source_key))
            order += 1

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
    for directories in folder_directories:
        for directory in directories:
            if is_excluded(directory):
                continue
            has_output = any(
                result.dst is not None and result.status in SIZE_STATUSES
                and (result.src == directory or directory in result.src.parents)
                for result in results
            )
            if has_output:
                continue
            if any(directory.iterdir()):
                skipped_directories.add(directory)
            else:
                empty_directories.add(directory)

    print("\n--- Done ---" if not opts.dry_run else "\n--- Dry run result ---")
    for line in summarize(results, opts.dry_run):
        print(line)

    failures = failure_lines(results)
    if failures:
        print(f"\n--- Failed files ({len(failures)}) ---")
        for line in failures:
            print(line)
    for directory in sorted(empty_directories):
        print(f"[EMPTY] {directory}")
    for directory in sorted(skipped_directories):
        print(f"[SKIP DIR] {directory} (no output files)")

    if not opts.dry_run:
        try:
            write_log(log_path, input_paths, dst_dir, opts, args.recursive, workers,
                      results, started, interrupted, skipped_inputs,
                      sorted(empty_directories), sorted(skipped_directories))
            print(f"\nLog written to: {log_path}")
        except OSError as e:
            print(f"\nCould not write log file {log_path}: {e}")
        if report_path is not None:
            try:
                write_report(report_path, input_paths, dst_dir, opts, args.recursive,
                             workers, results, started, interrupted)
                print(f"\nReport written to: {report_path}")
            except OSError as e:
                print(f"\nCould not write report file {report_path}: {e}")

    if interrupted:
        return 130
    return 2 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
