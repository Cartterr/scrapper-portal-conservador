from pathlib import Path


def test_outputs_folder_placeholder_exists() -> None:
    assert Path("outputs/.gitkeep").is_file()
