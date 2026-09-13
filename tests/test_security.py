from app.security import StaticBearerAuthenticator, redact_text


def test_bearer_auth_is_exact() -> None:
    auth = StaticBearerAuthenticator("a" * 40)
    assert auth.authenticate("a" * 40)
    assert not auth.authenticate("a" * 39)
    assert not auth.authenticate("A" * 40)


def test_redaction_removes_secrets() -> None:
    assert redact_text("token=abc password=xyz", ["abc", "xyz"]) == (
        "token=[REDACTED] password=[REDACTED]"
    )
