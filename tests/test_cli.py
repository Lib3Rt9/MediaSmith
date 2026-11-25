import csv
import json
import sys

import pytest
from PIL import Image

from mediasmith.cli import DEFAULT_LOG_NAME, build_parser, main
from mediasmith.identity import COMMAND_NAME, PROJECT_NAME
from mediasmith.resume import MANIFEST_NAME

from conftest import make_noise


def populate(src):
    (src / "sub").mkdir()
    make_noise(src / "big.png", (3000, 2000))
    make_noise(src / "small.jpg", (400, 300), quality=95)
    make_noise(src / "sub" / "deep.png", (2800, 1800))
    (src / "bad.png").write_bytes(b"corrupt")
    (src / "notes.txt").write_text("hi")


def files_in(path):
    return sorted(p.relative_to(path).as_posix() for p in path.rglob("*") if p.is_file())


def test_project_identity_drives_cli_and_generated_names(capsys):
    assert build_parser().prog == COMMAND_NAME == PROJECT_NAME.lower()
    assert DEFAULT_LOG_NAME == f"{COMMAND_NAME}.log"
    assert MANIFEST_NAME == f".{COMMAND_NAME}-manifest.jsonl"
    with pytest.raises(SystemExit) as exc:
        main(["--version"])
    assert exc.value.code == 0
    assert capsys.readouterr().out.startswith(f"{COMMAND_NAME} ")


def test_end_to_end_with_failure_log_and_reports(folders):
    src, dst = folders
    populate(src)
    code = main([str(src), str(dst), "--recursive",
                 "--report", str(dst / "r.json")])
    assert code == 2  # one file failed
    names = files_in(dst)
    assert "big.jpg" in names and "small.jpg" in names and "sub/deep.jpg" in names
    log = (dst / DEFAULT_LOG_NAME).read_text()
    assert log.startswith(f"{PROJECT_NAME} ")
    assert "Recursive: True\n" in log
    assert "recursive=True" not in log
    assert "Failed files (1)" in log and "bad.png" in log and "UnidentifiedImageError" in log
    failure_details = log.split("--- Failed files", 1)[1]
    assert str(src / "bad.png") not in failure_details
    assert "cannot identify image file" in failure_details
    doc = json.loads((dst / "r.json").read_text())
    assert doc["summary"]["failed"] == 1 and doc["summary"]["converted"] == 2
    assert doc["setup"]["min_quality"] == 30
    assert doc["setup"]["workers"] == 1
    assert doc["setup"]["input"] == [str(src.resolve())]
    assert doc["setup"]["output"] == str(dst.resolve())
    assert doc["setup"]["report"] == str((dst / "r.json").resolve())
    assert doc["setup"]["copy_same"] is False
    statuses = {f["relative_path"]: f["status"] for f in doc["files"]}
    assert statuses["bad.png"] == "error" and "notes.txt" not in statuses


def test_show_setup_prints_effective_values_without_inputs(capsys):
    assert main(["--show-setup", "--workers", "2", "--quality", "88"]) == 0
    output = capsys.readouterr().out
    assert '"quality": 88' in output
    assert '"workers": 2' in output
    assert '"input": []' in output


def test_quiet_keeps_progress_but_hides_per_file_details(folders, capsys):
    src, dst = folders
    Image.new("RGB", (20, 20), (20, 30, 40)).save(src / "small.png")

    assert main([str(src / "small.png"), "--output", str(dst), "--quiet"]) == 0
    output = capsys.readouterr().out
    assert "[1/1]" in output
    assert "[CONVERT]" not in output and "[KEEP]" not in output and "[COPY]" not in output


@pytest.mark.parametrize("option", ["--no-progress", "--copy-jpg", "--jobs", "--rename", "--lossless"])
def test_removed_options_are_rejected(option):
    with pytest.raises(SystemExit):
        build_parser().parse_args([option])


def test_webp_lossless_format(tmp_path):
    source = tmp_path / "source.png"
    output = tmp_path / "output"
    Image.new("RGB", (20, 20), "red").save(source)

    assert main([str(source), "--output", str(output), "--format", "webp",
                 "--quiet"]) == 0
    assert (output / "source.webp").is_file()

    lossy_output = output / "lossless"
    assert main([str(source), "--output", str(lossy_output), "--format", "webp-lossless",
                 "--quiet"]) == 0
    assert (lossy_output / "source.webp").is_file()
    assert (output / "source.webp").is_file()


def test_csv_report(folders):
    src, dst = folders
    populate(src)
    main([str(src), str(dst), "--report", str(dst / "r.csv")])
    rows = list(csv.DictReader((dst / "r.csv").open(encoding="utf-8")))
    assert {r["relative_path"] for r in rows} >= {"big.png", "small.jpg", "bad.png"}
    assert [r for r in rows if r["status"] == "error"][0]["error"]


def test_custom_log_path(folders, tmp_path):
    src, dst = folders
    populate(src)
    main([str(src), str(dst), "--log", str(tmp_path / "my.log")])
    assert (tmp_path / "my.log").exists() and not (dst / DEFAULT_LOG_NAME).exists()


def test_dry_run_creates_nothing(folders):
    src, dst = folders
    populate(src)
    code = main([str(src), str(dst), "--recursive", "--dry-run"])
    assert code == 2 and not dst.exists()


def test_originals_never_modified(folders):
    src, dst = folders
    populate(src)
    before = {p: p.read_bytes() for p in src.rglob("*") if p.is_file()}
    main([str(src), str(dst), "--recursive"])
    assert before == {p: p.read_bytes() for p in src.rglob("*") if p.is_file()}


def test_resume_skips_finished_files(folders):
    src, dst = folders
    populate(src)
    args = [str(src), str(dst), "--recursive", "--resume", "--quiet"]
    main(args)
    first = files_in(dst)
    assert main(args) == 2                      # bad.png still fails, nothing else redone
    assert files_in(dst) == first               # no "_1" duplicates
    make_noise(src / "new.png", (2600, 1700))   # a new file is picked up
    main(args)
    assert "new.jpg" in files_in(dst)


def test_resume_distinguishes_selected_files_with_same_name(tmp_path):
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    Image.new("RGB", (20, 20), "red").save(first / "same.png")
    Image.new("RGB", (20, 20), "blue").save(second / "same.png")
    output = tmp_path / "out"
    args = [
        "-i", str(first / "same.png"), str(second / "same.png"),
        "-o", str(output), "--resume", "--quiet",
    ]

    assert main(args) == 0
    first_run = files_in(output)
    assert main(args) == 0
    assert files_in(output) == first_run
    assert len([name for name in first_run if name.startswith("same")]) == 2

    manifest = (output / MANIFEST_NAME).read_text()
    assert '"source":' not in manifest
    assert "source_key" in manifest


def test_output_folder_cannot_contain_selected_file(tmp_path):
    src = tmp_path / "photo.jpg"
    Image.new("RGB", (20, 20), "red").save(src)
    assert main([str(src), str(tmp_path), "--quiet"]) == 1


def test_resume_after_interrupt_style_partial_manifest(folders):
    src, dst = folders
    make_noise(src / "a.png", (3000, 2000))
    main([str(src), str(dst), "--quiet"])
    manifest = dst / MANIFEST_NAME
    with manifest.open("a") as fh:
        fh.write('{"half written')           # torn last line must not break loading
    assert main([str(src), str(dst), "--quiet", "--resume"]) == 0
    assert files_in(dst).count("a.jpg") == 1 and "a_1.jpg" not in files_in(dst)


def test_parallel_matches_sequential(folders, tmp_path):
    src, dst = folders
    for i in range(6):
        make_noise(src / f"p{i}.png", (2800, 1800))
    main([str(src), str(dst), "--quiet", "--workers", "1"])
    dst2 = tmp_path / "out2"
    main([str(src), str(dst2), "--quiet", "--workers", "4"])
    assert files_in(dst) == files_in(dst2)


def test_parallel_name_clash_is_safe(folders):
    src, dst = folders
    for ext in ("png", "bmp", "tiff"):
        make_noise(src / f"same.{ext}", (2800, 1800))
    main([str(src), str(dst), "--quiet", "--workers", "3"])
    assert sorted(n for n in files_in(dst) if n.endswith(".jpg")) == \
        ["same.jpg", "same_1.jpg", "same_2.jpg"]


def test_width_option_and_conflict(folders, capsys):
    src, dst = folders
    make_noise(src / "a.png", (3000, 2000))
    main([str(src), str(dst), "--quiet", "--width", "1000"])
    with Image.open(dst / "a.jpg") as im:
        assert im.size == (1000, 667)
    with pytest.raises(SystemExit):   # argparse rejects two resize options
        main([str(src), str(dst), "--width", "10", "--height", "10"])


def test_flexible_inputs_are_separated_and_indexed(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    first = tmp_path / "first"
    second = tmp_path / "second"
    first.mkdir()
    second.mkdir()
    Image.new("RGB", (20, 20), (20, 30, 40)).save(first / "one.png")
    Image.new("RGB", (20, 20), (20, 30, 40)).save(second / "two.png")
    Image.new("RGB", (20, 20), (20, 30, 40)).save(tmp_path / "loose.png")

    args = ["-i", str(first), str(second), str(tmp_path / "loose.png"),
            "-o", "processed", "--quiet"]
    assert main(args) == 0
    assert (tmp_path / "processed" / "first" / "one.png").is_file()
    assert (tmp_path / "processed" / "second" / "two.png").is_file()
    assert (tmp_path / "processed" / "loose.png").is_file()
    log = (tmp_path / "processed" / DEFAULT_LOG_NAME).read_text(encoding="utf-8")
    assert "Input files :\n  - loose.png\n" in log
    assert "Input folders:\n  - first\n  - second\n" in log
    assert "Input   :" not in log
    assert "Recursive: False\nOutput  : processed\n" in log
    assert "recursive=False" not in log
    assert "[KEEP]    first/one.png" in log
    assert str(tmp_path / "processed" / "first" / "one.png") not in log
    assert "Output  : processed\n" in log
    assert str(tmp_path) not in log

    assert main(args) == 0
    assert (tmp_path / "processed_1" / "first" / "one.png").is_file()


def test_recursive_parent_input_covers_child_folders_and_files(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    parent = tmp_path / "testpicture"
    child = parent / "testpic"
    child.mkdir(parents=True)
    parent_image = parent / "parent.jpg"
    child_image = child / "child.jpg"
    Image.new("RGB", (20, 20), (20, 30, 40)).save(parent_image)
    Image.new("RGB", (20, 20), (20, 30, 40)).save(child_image)

    args = [
        "-i", str(parent), str(child), str(parent_image),
        "-o", "processed", "--recursive", "--quiet",
    ]
    assert main(args) == 0

    output = tmp_path / "processed"
    assert (output / "testpicture" / "parent.jpg").is_file()
    assert (output / "testpicture" / "testpic" / "child.jpg").is_file()
    assert not (output / "testpic" / "child.jpg").exists()
    assert not (output / "parent.jpg").exists()

    log = (output / DEFAULT_LOG_NAME).read_text(encoding="utf-8")
    assert (
        "Input files :\n  - testpicture/parent.jpg\n"
        "Input folders:\n  - testpicture\n  - testpicture/testpic\n"
    ) in log
    assert f"{child}: already covered by input folder {parent}" in log
    assert f"{parent_image}: already covered by input folder {parent}" in log


def test_convert_only_ignores_resize_options_for_single_file(tmp_path):
    src = make_noise(tmp_path / "photo.png", (120, 80))
    out = tmp_path / "converted"
    assert main([str(src), "--output", str(out), "--convert-only",
                 "--max-edge", "10", "--quiet"]) == 0
    with Image.open(out / "photo.jpg") as converted:
        assert converted.size == (120, 80)
        assert converted.format == "JPEG"


def test_all_inputs_honor_name_exclusions(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    album = tmp_path / "album"
    (album / "skip").mkdir(parents=True)
    Image.new("RGB", (20, 20), (20, 30, 40)).save(album / "keep.png")
    Image.new("RGB", (20, 20), (20, 30, 40)).save(album / "skip" / "hidden.png")
    Image.new("RGB", (20, 20), (20, 30, 40)).save(tmp_path / "skip.png")

    assert main(["--all", "--recursive", "--exclude", "skip", "--exclude", "skip.png",
                 "--output", "processed", "--quiet"]) == 0
    assert (tmp_path / "processed" / "album" / "keep.png").is_file()
    assert not (tmp_path / "processed" / "album" / "skip" / "hidden.png").exists()
    assert not (tmp_path / "processed" / "skip.png").exists()


def test_exclude_extension_is_case_insensitive_and_reported_in_setup(tmp_path):
    source = tmp_path / "photos"
    source.mkdir()
    Image.new("RGB", (20, 20), (20, 30, 40)).save(source / "skip.PNG", format="PNG")
    Image.new("RGB", (20, 20), (20, 30, 40)).save(source / "keep.jpg", format="JPEG")

    output = tmp_path / "processed"
    report = output / "report.json"
    assert main([
        str(source), "--output", str(output), "--quiet",
        "--exclude-extension", "PNG", "--report", str(report),
    ]) == 0

    assert (output / "photos" / "keep.jpg").is_file()
    assert not (output / "photos" / "skip.jpg").exists()
    setup = json.loads(report.read_text(encoding="utf-8"))["setup"]
    assert setup["exclude_extensions"] == [".png"]


def test_bad_arguments_return_1(folders, capsys):
    src, dst = folders
    assert main([str(src / "missing"), str(dst)]) == 1
    assert main([str(src), str(dst), "--quality", "500"]) == 1
    assert main([str(src), str(dst), "--scale", "200"]) == 1
    assert main([str(src), str(dst), "--report", "x.txt"]) == 1
    assert main([str(src), str(src)]) == 1
    assert "error:" in capsys.readouterr().err


@pytest.mark.skipif(sys.version_info < (3, 11), reason="TOML config needs 3.11+ or tomli")
def test_config_file_and_cli_override(folders, tmp_path):
    src, dst = folders
    make_noise(src / "a.png", (3000, 2000))
    cfg = tmp_path / "ms.toml"
    cfg.write_text('max-edge = 1000\nquality = 80\nformat = "webp"\n')
    main([str(src), str(dst), "--quiet", "--config", str(cfg)])
    with Image.open(dst / "a.webp") as im:
        assert im.size == (1000, 667)
    dst2 = tmp_path / "out2"
    main([str(src), str(dst2), "--quiet", "--config", str(cfg), "--width", "600"])
    with Image.open(dst2 / "a.webp") as im:
        assert im.size == (600, 400)   # CLI size option beats the config's max-edge


def test_json_config_and_unknown_key(folders, tmp_path):
    src, dst = folders
    make_noise(src / "a.png", (3000, 2000))
    cfg = tmp_path / "ms.json"
    cfg.write_text('{"max_edge": 800}')
    main([str(src), str(dst), "--quiet", "--config", str(cfg)])
    with Image.open(dst / "a.jpg") as im:
        assert max(im.size) == 800
    cfg.write_text('{"nonsense": 1}')
    assert main([str(src), str(dst), "--config", str(cfg)]) == 1


def test_target_size_via_cli(folders):
    src, dst = folders
    make_noise(src / "a.png", (1200, 800))
    main([str(src), str(dst), "--quiet", "--target-size", "300KB",
          "--min-quality", "5"])
    assert (dst / "a.jpg").stat().st_size <= 300 * 1024
