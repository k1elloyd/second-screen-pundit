"""A deploy always loads its own page files: every asset URL carries the build's version."""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from second_screen_pundit.api import asset_version, create_app
from tests.test_api import _settings

WEB = Path(__file__).resolve().parents[1] / "src" / "second_screen_pundit" / "web"
NODE = shutil.which("node")


def test_the_page_versions_its_script_stylesheet_and_audio_and_is_always_revalidated() -> None:
    version = asset_version()
    client = TestClient(create_app(settings=_settings()))
    response = client.get("/")
    assert response.status_code == 200
    assert response.headers["cache-control"] == "no-cache"
    html = response.text
    assert f'src="/static/app.js?v={version}"' in html
    assert f'href="/static/styles.css?v={version}"' in html
    assert f'<meta name="asset-version" content="{version}">' in html
    assert 'src="/static/app.js"' not in html and 'href="/static/styles.css"' not in html
    # The versioned URLs serve the same files.
    for path in ("app.js", "styles.css", "audio/crowd-bed.mp3", "audio/goal-roar.mp3"):
        served = client.get(f"/static/{path}?v={version}")
        assert served.status_code == 200, path
        assert served.content == (WEB / path).read_bytes()


def test_the_version_changes_when_any_page_file_changes(tmp_path: Path) -> None:
    web = tmp_path / "web"
    shutil.copytree(WEB, web, ignore=shutil.ignore_patterns("__pycache__"))
    before = asset_version(web)
    assert before == asset_version(WEB)
    for name in ("app.js", "styles.css", "audio/goal-roar.mp3"):
        path = web / name
        original = path.read_bytes()
        path.write_bytes(original + b"\n")
        assert asset_version(web) != before, name
        path.write_bytes(original)
    assert asset_version(web) == before


@pytest.mark.skipif(NODE is None, reason="Node.js is needed to run the page's code")
def test_the_crowd_audio_urls_carry_the_page_version() -> None:
    source = (WEB / "app.js").read_text(encoding="utf-8")
    start = source.index("// The server stamps the page")
    end = source.index("const CROWD_LOOP_CROSSFADE_SECONDS")
    script = (
        "globalThis.document = { querySelector: (s) => s === 'meta[name=\"asset-version\"]'"
        " ? { content: 'abc123' } : null };\n"
        + source[start:end]
        + "\nconsole.log(CROWD_BED_URL + ' ' + CROWD_ROAR_URL);"
    )
    output = subprocess.run(
        [NODE, "-"], input=script, capture_output=True, text=True, timeout=30  # type: ignore[list-item]
    )
    assert output.returncode == 0, output.stderr
    assert output.stdout.split() == [
        "/static/audio/crowd-bed.mp3?v=abc123",
        "/static/audio/goal-roar.mp3?v=abc123",
    ]
