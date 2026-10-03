from rlab import worker


def test_worker_env_strips_cloud_creds_and_adds_token(tmp_path, monkeypatch):
    monkeypatch.setenv("AWS_SECRET_ACCESS_KEY", "x")
    monkeypatch.setattr(worker, "TOKEN_FILE", tmp_path / "missing")
    env = worker.worker_env()
    assert "AWS_SECRET_ACCESS_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    token = tmp_path / "oauth_token"
    token.write_text("sk-ant-oat-test\n")
    monkeypatch.setattr(worker, "TOKEN_FILE", token)
    assert worker.worker_env()["CLAUDE_CODE_OAUTH_TOKEN"] == "sk-ant-oat-test"
