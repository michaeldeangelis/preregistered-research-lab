import json

import pytest


@pytest.fixture(autouse=True)
def private_kernel_state(tmp_path, monkeypatch):
    """Tests never touch the real split salt, worker config dirs or user settings."""
    monkeypatch.setattr("rlab.kernel.SALT_FILE", tmp_path / "salt")
    monkeypatch.setattr("rlab.kernel.WORKER_HOME", tmp_path / "worker-home")
    settings = tmp_path / "user-settings.json"
    settings.write_text(json.dumps({"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [
        {"type": "command", "command": "python3 /nonexistent/spend_guard.py"}]}]}}))
    monkeypatch.setattr("rlab.kernel.USER_SETTINGS", settings)
