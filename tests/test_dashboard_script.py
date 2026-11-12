"""Exercise the JavaScript actually rendered by Jinja, with no npm dependencies."""
import json
import os
from pathlib import Path
import re
import shutil
import subprocess

import pytest

import main


def test_dashboard_refresh_and_error_recovery():
    node = os.environ.get("NODE_BINARY") or shutil.which("node")
    if not node:
        pytest.skip("Node.js is optional locally; CI installs it for the dashboard test")
    html = main.templates.env.get_template("index.html").render(
        data=None, sample_interval_seconds=10,
    )
    match = re.search(r"<script>([\s\S]*?)</script>", html)
    assert match, "Dashboard JavaScript not found"
    harness = Path(__file__).with_name("check_dashboard.cjs")
    result = subprocess.run(
        [node, str(harness)], input=json.dumps({"source": match.group(1)}),
        text=True, capture_output=True, timeout=30,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert "PASS: dashboard refresh" in result.stdout
