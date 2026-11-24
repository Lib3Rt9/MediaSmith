import importlib.util
import sys
from pathlib import Path

import pytest
from PIL import Image


SCRIPT = Path(__file__).parents[1] / "prototype" / "mediasmith.py"


def run_cli(module, inputs, monkeypatch, options=(), selector="--input"):
    monkeypatch.setattr(
        sys, "argv",
        ["mediasmith.py", selector, *(str(path) for path in inputs), *options],
    )
    return module.main()


@pytest.fixture
def prototype(tmp_path):
    spec = importlib.util.spec_from_file_location("prototype_mediasmith", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    module.__file__ = str(tmp_path / "mediasmith.py")
    return module


def test_multiple_inputs_use_indexed_output_and_log_file_results(
    prototype, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    inputs = []
    for folder in ("f1", "f2"):
        src = tmp_path / folder
        src.mkdir()
        Image.new("RGB", (20, 20), (20, 30, 40)).save(src / f"{folder}.jpg")
        (src / "notes.txt").write_text("not an image")
        inputs.append(src)

    Image.effect_noise((300, 200), 100).convert("RGB").save(inputs[0] / "noise.png")
    assert run_cli(prototype, inputs, monkeypatch, ["--max-edge", "100"]) == 0

    output = tmp_path / "output"
    assert (output / "f1" / "f1.jpg").is_file()
    assert (output / "f2" / "f2.jpg").is_file()
    log = (tmp_path / prototype.DEFAULT_LOG_NAME).read_text(encoding="utf-8")
    assert "--- Files ---" in log
    assert "[COPY]    f1/f1.jpg" in log
    assert "[COPY]    f2/f2.jpg" in log
    assert "[CONVERT] f1/noise.png -> noise.jpg  300x200 -> 100x67  (" in log
    assert "[SKIP]    f1/notes.txt" in log
    assert "Converted/re-saved: 1  (of which resized: 1)" in log

    assert run_cli(prototype, inputs, monkeypatch) == 0
    assert (tmp_path / "output_1" / "f1" / "f1.jpg").is_file()
    assert "Output folder:" in capsys.readouterr().out


def test_custom_log_option_is_removed(prototype, tmp_path):
    with pytest.raises(SystemExit):
        prototype.build_parser().parse_args(
            ["--input", str(tmp_path), "--log", str(tmp_path / "custom.log")]
        )


def test_inputs_with_same_name_get_distinct_output_subfolders(
    prototype, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    inputs = []
    for parent in ("first", "second"):
        src = tmp_path / parent / "album"
        src.mkdir(parents=True)
        Image.new("RGB", (20, 20), (20, 30, 40)).save(src / "photo.jpg")
        inputs.append(src)

    assert run_cli(prototype, inputs, monkeypatch) == 0
    assert (tmp_path / "output" / "album" / "photo.jpg").is_file()
    assert (tmp_path / "output" / "album_1" / "photo.jpg").is_file()


def test_custom_output_folder_name_and_conflict_index(
    prototype, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "pictures"
    src.mkdir()
    Image.new("RGB", (20, 20), (20, 30, 40)).save(src / "photo.jpg")

    assert run_cli(prototype, [src], monkeypatch, ["--output", "processed"]) == 0
    assert (tmp_path / "processed" / "pictures" / "photo.jpg").is_file()

    assert run_cli(prototype, [src], monkeypatch, ["-o", "processed"]) == 0
    assert (tmp_path / "processed_1" / "pictures" / "photo.jpg").is_file()


def test_missing_input_is_skipped_and_logged_with_setup(
    prototype, tmp_path, monkeypatch, capsys
):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "valid.png"
    Image.new("RGB", (80, 60), (20, 30, 40)).save(src)
    missing = tmp_path / "missing"

    assert run_cli(
        prototype, [missing, src], monkeypatch,
        ["--output", "results", "--quality", "88", "--convert-only"],
    ) == 0

    output = tmp_path / "results"
    with Image.open(output / "valid.jpg") as converted:
        assert converted.size == (80, 60)
        assert converted.format == "JPEG"
    log = (tmp_path / prototype.DEFAULT_LOG_NAME).read_text(encoding="utf-8")
    assert f"{missing}: not found or not a file/folder" in log
    assert "format=jpg max-edge=2560 quality=88 convert-only=True" in log
    assert "Input files : missing, valid.png" in log
    assert "Input folders: None" in log
    assert "exclude=None" in log
    assert f"Skipping missing or invalid input: {missing}" in capsys.readouterr().out


def test_single_file_input_writes_to_output_root(prototype, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    src = tmp_path / "photo.png"
    Image.new("RGB", (40, 30), (30, 40, 50)).save(src)

    assert run_cli(
        prototype, [src], monkeypatch, ["--output", "converted", "--convert-only"],
    ) == 0
    assert (tmp_path / "converted" / "photo.jpg").is_file()


def test_no_input_selector_displays_help(prototype, tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(sys, "argv", ["mediasmith.py"])

    assert prototype.main() == 0
    assert "--input" in capsys.readouterr().out
    assert not (tmp_path / "output").exists()


def test_all_images_beside_script_are_selected(prototype, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Image.new("RGB", (40, 30), (30, 40, 50)).save(tmp_path / "near_script.png")
    (tmp_path / "notes.txt").write_text("not an image")
    monkeypatch.setattr(sys, "argv", [
        "mediasmith.py", "--all", "--output", "all_output", "--convert-only"
    ])

    assert prototype.main() == 0
    assert (tmp_path / "all_output" / "near_script.jpg").is_file()
    assert not (tmp_path / "all_output" / "notes.txt").exists()


def test_input_selector_auto_detects_files_and_folders(prototype, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    image = tmp_path / "not_a_folder.png"
    Image.new("RGB", (20, 20), (10, 20, 30)).save(image)

    assert run_cli(prototype, [image], monkeypatch, ["--convert-only"]) == 0
    assert (tmp_path / "output" / "not_a_folder.jpg").is_file()


def test_positional_paths_are_accepted_without_input_option(
    prototype, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    folder = tmp_path / "album"
    folder.mkdir()
    Image.new("RGB", (20, 20), (20, 30, 40)).save(folder / "folder.png")
    image = tmp_path / "single.jpg"
    Image.new("RGB", (30, 20), (20, 30, 40)).save(image)

    monkeypatch.setattr(sys, "argv", [
        "mediasmith.py", str(folder), str(image),
        "--convert-only", "--output", "converted",
    ])

    assert prototype.main() == 0
    assert (tmp_path / "converted" / "album" / "folder.jpg").is_file()
    assert (tmp_path / "converted" / "single.jpg").is_file()


def test_argument_forms_can_be_mixed_without_conflict(prototype, tmp_path):
    positional = tmp_path / "positional.png"
    selected = tmp_path / "selected.png"
    repeated = tmp_path / "repeated.png"

    parser = prototype.build_parser()

    cases = (
        ([str(positional)], [positional], None),
        ([str(positional), "--convert-only"], [positional], None),
        ([str(positional), "--recursive", "--exclude", "skip"],
         [positional], None),
        ([str(positional), "--output", "processed"],
         [positional], None),
        (["-i", str(selected)], [], [[selected]]),
        (["-i", str(selected), "--recursive", "--quality", "80"],
         [], [[selected]]),
        ([str(positional), "-i", str(selected), "--all", "--convert-only"],
         [positional], [[selected]]),
        (["-i", str(selected), "-i", str(repeated), "--recursive", "--output", "done"],
         [], [[selected], [repeated]]),
    )

    for argv, expected_positional, expected_input in cases:
        args = parser.parse_args(argv)
        assert args.positional_paths == expected_positional
        assert args.input_paths == expected_input
def test_all_folders_logs_empty_directories_and_skips_exclusions(
    prototype, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    album = tmp_path / "album"
    album.mkdir()
    Image.new("RGB", (40, 30), (30, 40, 50)).save(album / "keep.png")
    (album / "skip").mkdir()
    Image.new("RGB", (40, 30), (30, 40, 50)).save(album / "skip" / "photo.png")
    empty = tmp_path / "empty"
    empty.mkdir()

    monkeypatch.setattr(sys, "argv", [
        "mediasmith.py", "--all", "--recursive", "--exclude", "skip",
        "--output", "processed", "--convert-only",
    ])
    assert prototype.main() == 0

    output = tmp_path / "processed"
    assert (output / "album" / "keep.jpg").is_file()
    assert not (output / "album" / "skip" / "photo.jpg").exists()
    assert not (output / "empty").exists()
    assert not (output / "album" / "skip").exists()
    log = (tmp_path / prototype.DEFAULT_LOG_NAME).read_text(encoding="utf-8")
    assert f"[EMPTY] {empty}" in log
    assert f"[EMPTY] {album / 'skip'}" not in log


def test_all_images_excludes_named_files(prototype, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Image.new("RGB", (40, 30), (30, 40, 50)).save(tmp_path / "keep.png")
    Image.new("RGB", (40, 30), (30, 40, 50)).save(tmp_path / "skip.png")
    monkeypatch.setattr(sys, "argv", [
        "mediasmith.py", "--all", "--exclude", "skip.png",
        "--output", "processed", "--convert-only",
    ])

    assert prototype.main() == 0
    assert (tmp_path / "processed" / "keep.jpg").is_file()
    assert not (tmp_path / "processed" / "skip.jpg").exists()


def test_input_and_all_can_be_combined(prototype, tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    selected_folder = tmp_path / "selected"
    selected_folder.mkdir()
    Image.new("RGB", (20, 20), (20, 30, 40)).save(selected_folder / "folder.png")
    selected_file = tmp_path / "specific.png"
    Image.new("RGB", (20, 20), (20, 30, 40)).save(selected_file)
    Image.new("RGB", (20, 20), (20, 30, 40)).save(tmp_path / "beside_script.png")

    monkeypatch.setattr(sys, "argv", [
        "mediasmith.py", "-i", str(selected_folder), str(selected_file),
        "--all", "-o", "combined", "--convert-only",
    ])
    assert prototype.main() == 0

    output = tmp_path / "combined"
    assert (output / "selected" / "folder.jpg").is_file()
    assert (output / "specific.jpg").is_file()
    assert (output / "beside_script.jpg").is_file()
    log = (tmp_path / prototype.DEFAULT_LOG_NAME).read_text(encoding="utf-8")
    assert "Input files : specific.png, beside_script.png" in log
    assert "Input folders: selected" in log


def test_duplicate_inputs_are_deduplicated_and_exclusions_are_described(
    prototype, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    image = tmp_path / "photo.png"
    Image.new("RGB", (20, 20), (20, 30, 40)).save(image)
    monkeypatch.setattr(sys, "argv", [
        "mediasmith.py", "-i", str(image), "--all", "--output", "processed",
        "--exclude", "skip.png", "--convert-only",
    ])

    assert prototype.main() == 0

    log = (tmp_path / prototype.DEFAULT_LOG_NAME).read_text(encoding="utf-8")
    assert "Input files : photo.png" in log
    assert "Input folders: None" in log
    assert "exclude=[file/folder name] skip.png" in log


def test_all_invalid_inputs_are_logged_without_creating_output(
    prototype, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    missing = tmp_path / "missing.png"
    unsupported = tmp_path / "notes.txt"
    unsupported.write_text("not an image")

    assert run_cli(
        prototype, [missing, unsupported], monkeypatch, ["--output", "unused"]
    ) == 1

    assert not (tmp_path / "unused").exists()
    log = (tmp_path / prototype.DEFAULT_LOG_NAME).read_text(encoding="utf-8")
    assert f"{missing}: not found or not a file/folder" in log
    assert f"{unsupported}: not a supported image file" in log
    assert "Output  :" in log and "(not created)" in log


def test_folder_with_no_output_files_is_logged_but_not_created(
    prototype, tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    source = tmp_path / "text-only"
    source.mkdir()
    (source / "notes.txt").write_text("not an image")

    assert run_cli(prototype, [source], monkeypatch) == 0

    assert not (tmp_path / "output").exists()
    log = (tmp_path / prototype.DEFAULT_LOG_NAME).read_text(encoding="utf-8")
    assert f"[SKIP DIR] {source}" in log
