"""Local JSONL persistence, isolation and retention checks."""

import json
import os
import subprocess
import sys
import time
from pathlib import Path
from unittest.mock import patch

from invoice_intelligence.config.logging import _prune_stale_files

_EMIT = """
import logging
import sys
from invoice_intelligence.config.logging import configure_logging
from invoice_intelligence.config.settings import Settings
settings = Settings(
    _env_file=None,
    log_directory=sys.argv[1],
    log_file_enabled=None if sys.argv[2] == 'auto' else sys.argv[2] == 'true',
)
configure_logging(settings, component='api')
logging.getLogger('invoice_intelligence.test').info(
    'telemetry_span_finished',
    extra={'trace_id': 'trace-a', 'tenant_id': 'tenant-a', 'stage': 'vision'},
)
if len(sys.argv) > 3 and sys.argv[3] == 'rotate':
    from logging.handlers import RotatingFileHandler
    handler = next(
        item for item in logging.getLogger().handlers
        if isinstance(item, RotatingFileHandler)
    )
    handler.maxBytes = 300
    for _ in range(5):
        logging.getLogger('invoice_intelligence.test').info('telemetry_span_finished')
"""


def test_local_file_and_console_share_safe_json(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _EMIT, str(tmp_path), "auto"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    files = list((tmp_path / "api").glob("api-*.jsonl"))
    assert len(files) == 1
    from_file = json.loads(files[0].read_text(encoding="utf-8").splitlines()[0])
    from_console = json.loads(result.stdout.splitlines()[0])
    assert from_file == from_console
    assert from_file["trace_id"] == "trace-a"
    assert from_file["tenant_id"] == "tenant-a"


def test_file_output_can_be_disabled(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _EMIT, str(tmp_path), "false"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert not (tmp_path / "api").exists()
    assert json.loads(result.stdout.splitlines()[0])["trace_id"] == "trace-a"


def test_file_handler_rotates_without_changing_console_output(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-c", _EMIT, str(tmp_path), "true", "rotate"],
        capture_output=True, text=True, check=False,
    )
    assert result.returncode == 0
    assert len(result.stdout.splitlines()) == 6
    assert list((tmp_path / "api").glob("api-*.jsonl.1"))


def test_retention_only_removes_stopped_component_logs(tmp_path: Path) -> None:
    component_dir = tmp_path / "api"
    component_dir.mkdir()
    old = component_dir / "api-1234.jsonl.1"
    current = component_dir / "api-5678.jsonl"
    unrelated = component_dir / "other-1234.jsonl"
    for path in (old, current, unrelated):
        path.write_text("{}\n", encoding="utf-8")
        timestamp = time.time() - 9 * 86_400
        os.utime(path, (timestamp, timestamp))
    with patch(
        "invoice_intelligence.config.logging._process_alive",
        side_effect=lambda pid: pid == 5678,
    ):
        _prune_stale_files(component_dir, "api", 7)
    assert not old.exists()
    assert current.exists()
    assert unrelated.exists()
