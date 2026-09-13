import pytest

from app.config import ConfigurationError, Settings


def test_config_rejects_weak_token(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("GMX_EMAIL", "user@example.com")
    monkeypatch.setenv("GMX_APP_PASSWORD", "secret")
    monkeypatch.setenv("MCP_API_TOKEN", "short")
    monkeypatch.setenv("MCP_HOSTNAME", "mail.example.com")
    with pytest.raises(ConfigurationError):
        Settings.from_env()


def test_config_reads_secret_file(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    monkeypatch.chdir(tmp_path)
    password = tmp_path / "password"
    password.write_text("app-password\n", encoding="utf-8")
    token = tmp_path / "token"
    token.write_text("t" * 40, encoding="utf-8")
    monkeypatch.setenv("GMX_EMAIL", "user@example.com")
    monkeypatch.delenv("MCP_API_TOKEN", raising=False)
    monkeypatch.delenv("GMX_APP_PASSWORD", raising=False)
    monkeypatch.setenv("GMX_APP_PASSWORD_FILE", str(password))
    monkeypatch.setenv("MCP_API_TOKEN_FILE", str(token))
    monkeypatch.setenv("MCP_HOSTNAME", "mail.example.com")
    settings = Settings.from_env()
    assert settings.gmx_app_password == "app-password"
    assert settings.mcp_api_token == "t" * 40


def test_config_loads_dotenv_without_overriding_environment(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    (tmp_path / ".env").write_text(
        "GMX_EMAIL=dotenv@example.com\nGMX_APP_PASSWORD=dotenv-pass\n"
        "MCP_API_TOKEN=" + "d" * 40 + "\nMCP_HOSTNAME=dotenv.example.com\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    settings = Settings.from_env()
    assert settings.gmx_email == "dotenv@example.com"
    monkeypatch.setenv("GMX_EMAIL", "explicit@example.com")
    assert Settings.from_env().gmx_email == "explicit@example.com"
