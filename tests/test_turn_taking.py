"""Turn-taking in the browser, run from pytest so there is one way in.

The logic that decides whether a caller may interrupt lives in the page, in
JavaScript, and it is the highest-risk code in the demo: it is what separates a
phone call from a walkie-talkie, and it is the one part with no Python to test.
`tests/js/turntaking.test.mjs` extracts the real `<script>` block from
`index.html` and drives it under a DOM/Audio shim.

**It runs the page's own source, not a copy of it.** A test that re-implemented
`isBargeIn` would agree with itself forever while the page was broken. Writing
it this way caught two faults on the first run, and both were total:

  * the sequence number was claimed BEFORE `stopSpeaking()` bumped it, so every
    clip was skipped on the first check and the desk never spoke at all;
  * `pause()` does not fire `ended`, so an interrupted utterance awaited a
    promise that never settled -- `speak` never returned and the clips queued
    behind the interruption were never silenced.

Skipped rather than failed where node is absent, because the Python suite must
stay runnable on a bare checkout. GitHub's ubuntu runners ship node, so this is
a real gate in CI and a convenience locally.
"""

from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
HARNESS = REPO / "tests" / "js" / "turntaking.test.mjs"
PAGE = REPO / "src" / "voicedesk" / "demo" / "index.html"

node = shutil.which("node")


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_browser_turn_taking() -> None:
    assert HARNESS.is_file(), HARNESS
    assert PAGE.is_file(), PAGE

    result = subprocess.run(  # noqa: S603 - fixed argv, no shell, repo-local paths
        [str(node), str(HARNESS), str(PAGE)],
        capture_output=True,
        text=True,
        timeout=120,
        cwd=REPO,
    )
    if result.returncode != 0:
        pytest.fail(f"{result.stdout}\n{result.stderr}")


@pytest.mark.skipif(node is None, reason="node is not installed")
def test_the_page_script_parses() -> None:
    """A syntax error in the page is a blank demo and a silent one.

    Nothing else in this repo would notice: the file is served as bytes and
    the failure appears only in a browser console nobody is watching.
    """
    source = PAGE.read_text(encoding="utf-8")
    body = source.split('<script type="module">')[1].split("</script>")[0]
    check = REPO / ".page-syntax-check.mjs"
    check.write_text(body.replace("import ", "// import ", 1), encoding="utf-8")
    try:
        result = subprocess.run(  # noqa: S603 - fixed argv, no shell
            [str(node), "--check", str(check)],
            capture_output=True,
            text=True,
            timeout=60,
        )
        if result.returncode != 0:
            pytest.fail(result.stderr)
    finally:
        check.unlink(missing_ok=True)
