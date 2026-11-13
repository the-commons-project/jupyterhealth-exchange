"""Which mail backend a deployment gets, from whether it has a relay and whether it is in
development."""

from jhe.settings import email_backend


def test_smtp_when_a_relay_is_configured():
    assert email_backend("smtp.example.org", debug=False).endswith("smtp.EmailBackend")
    assert email_backend("smtp.example.org", debug=True).endswith("smtp.EmailBackend")


def test_console_only_in_development():
    assert email_backend(None, debug=True).endswith("console.EmailBackend")


def test_dropped_not_logged_without_a_relay_outside_development():
    """Outside development, mail that cannot be sent is dropped, never printed to the log, where
    every message would carry its recipient's address and links."""
    assert email_backend(None, debug=False).endswith("dummy.EmailBackend")
    assert email_backend("", debug=False).endswith("dummy.EmailBackend")
