"""Diagnostic log: credential redaction, the rotating file, windowed builds."""

import logging
import sys

import pytest

from chattersyn.core.applog import LOG_NAME, redact, setup_logging


@pytest.fixture
def root_logger():
    root = logging.getLogger()
    handlers, level = list(root.handlers), root.level
    yield root
    for handler in list(root.handlers):
        if handler not in handlers:
            root.removeHandler(handler)
            handler.close()
    root.setLevel(level)


@pytest.mark.parametrize(("text", "expected"), [
    ("PASS oauth:abcdef0123456789abcdef01234567", "PASS oauth:<redacted>"),
    ("Authorization: Bearer abcdefghij0123456789klmnop", "Authorization: Bearer <redacted>"),
    ("Authorization: OAuth abcdefghij0123456789klmnop", "Authorization: OAuth <redacted>"),
    ("{'access_token': 'tok123', 'refresh_token': 'ref456'}",
     "{'access_token': '<redacted>', 'refresh_token': '<redacted>'}"),
    ('{"client_secret":"GOCSPX-abc"}', '{"client_secret":"<redacted>"}'),
    ("GET http://localhost:17821/?code=abc123&state=xyz",
     "GET http://localhost:17821/?code=<redacted>&state=xyz"),
    ("key=AIzaSyA-1234567890abcdefghijklmnopqrstu", "key=<redacted-api-key>"),
    ("token ya29.a0AfH6SMBx-y_z", "token ya29.<redacted>"),
])
def test_redact_hides_credentials(text, expected):
    assert redact(text) == expected


@pytest.mark.parametrize("text", [
    "HTTP status code: 404",
    "OAuth sign-in started",
    "joined #forsen as justinfan12345",
])
def test_redact_keeps_ordinary_text(text):
    assert redact(text) == text


def test_log_file_gets_records_with_credentials_redacted(tmp_path, root_logger):
    path = setup_logging(tmp_path / "diagnostics")
    assert path == tmp_path / "diagnostics" / LOG_NAME
    logging.getLogger("chattersyn.test").warning("sending PASS oauth:secret123abc")
    for handler in root_logger.handlers:
        handler.flush()
    text = path.read_text(encoding="utf-8")
    assert "PASS oauth:<redacted>" in text
    assert "secret123abc" not in text


def test_per_request_httpx_lines_stay_out_of_the_log(tmp_path, root_logger, monkeypatch):
    monkeypatch.setattr(logging.getLogger("httpx"), "level", logging.NOTSET)
    setup_logging(tmp_path)
    assert not logging.getLogger("httpx").isEnabledFor(logging.INFO)
    assert logging.getLogger("httpx").isEnabledFor(logging.WARNING)


def test_setting_up_twice_keeps_one_set_of_handlers(tmp_path, root_logger):
    setup_logging(tmp_path)
    count = len(root_logger.handlers)
    setup_logging(tmp_path)
    assert len(root_logger.handlers) == count


def test_windowed_build_without_stderr_logs_to_the_file_only(tmp_path, root_logger, monkeypatch):
    monkeypatch.setattr(sys, "stderr", None)  # PyInstaller windowed mode on Windows
    setup_logging(tmp_path)
    ours = [h for h in root_logger.handlers if getattr(h, "_chattersyn", False)]
    assert [type(h).__name__ for h in ours] == ["RotatingFileHandler"]
