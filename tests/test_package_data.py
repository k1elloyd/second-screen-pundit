"""The installed package carries the whole page, audio included.

The image runs `pip install .`, so the app serves web/ from site-packages, not the source
tree; anything the package data leaves out is a 404 there and still works locally. The
crowd MP3s were missed once because the glob `web/*` doesn't reach into web/audio.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
WEB = REPO / "src" / "second_screen_pundit" / "web"
# What the Dockerfile copies before `pip install .`.
DOCKER_SOURCES = ("pyproject.toml", "README.md", "LICENSE", "third_party", "src")
CROWD_AUDIO = ("crowd-bed.mp3", "goal-roar.mp3")


def _build_wheel(tmp_path: Path) -> zipfile.ZipFile:
    source = tmp_path / "source"
    source.mkdir()
    for name in DOCKER_SOURCES:
        path = REPO / name
        if path.is_dir():
            shutil.copytree(
                path, source / name, ignore=shutil.ignore_patterns("__pycache__", "*.egg-info")
            )
        else:
            shutil.copy2(path, source / name)
    out = tmp_path / "wheel"
    subprocess.run(
        [
            sys.executable, "-m", "pip", "wheel", str(source),
            "--no-deps", "--no-build-isolation", "--no-index", "-q", "-w", str(out),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    (wheel,) = out.glob("second_screen_pundit-*.whl")
    return zipfile.ZipFile(wheel)


def test_the_wheel_contains_the_crowd_audio_and_every_web_file(tmp_path: Path) -> None:
    names = set(_build_wheel(tmp_path).namelist())
    for mp3 in CROWD_AUDIO:
        assert f"second_screen_pundit/web/audio/{mp3}" in names, f"{mp3} is not packaged"
    web_files = [
        path.relative_to(WEB.parent.parent).as_posix()
        for path in WEB.rglob("*")
        if path.is_file() and "__pycache__" not in path.parts
    ]
    missing = [name for name in web_files if name not in names]
    assert not missing, f"web files left out of the package: {missing}"
