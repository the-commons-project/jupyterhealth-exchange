from pathlib import Path

import django
from django.conf import settings
from django.template.loader import render_to_string

STATIC = Path(settings.BASE_DIR) / "core" / "static"


def test_inter_woff2_weights_are_vendored():
    font_dir = STATIC / "common" / "css" / "fonts" / "inter"
    for weight in (400, 500, 600, 700):
        f = font_dir / f"inter-{weight}.woff2"
        assert f.exists(), f"missing vendored font {f}"
        # A real woff2 starts with the signature 'wOF2' and is not a tiny stub.
        assert f.read_bytes()[:4] == b"wOF2", f"{f} is not a woff2 file"
        assert f.stat().st_size > 10_000, f"{f} looks like a stub, not a real font"


def test_patient_facing_css_defines_tokens_and_font():
    css = (STATIC / "common" / "css" / "patient-facing.css").read_text()
    # @font-face wired to the vendored files, no external URL.
    assert "@font-face" in css and "fonts/inter/inter-400.woff2" in css
    assert "https://" not in css, "no external fetches allowed under strict CSP"
    # The rebrand surface: every token the templates rely on is declared once.
    for token in (
        "--pf-bg", "--pf-surface", "--pf-ink", "--pf-muted", "--pf-accent",
        "--pf-accent-ink", "--pf-line", "--pf-radius", "--pf-radius-lg",
        "--pf-font", "--pf-maxw",
    ):
        assert token in css, f"missing design token {token}"
    # Component hooks the templates use.
    for cls in (".pf-page", ".pf-header", ".pf-eyebrow", ".pf-h1", ".pf-lede",
                ".pf-card", ".pf-card__badge", ".pf-log"):
        assert cls in css, f"missing component class {cls}"


def test_patient_facing_base_links_stylesheet_and_wraps_page():
    html = render_to_string(
        "common/patient_facing/_test_probe.html",
        {"SITE_TITLE": "JupyterHealth Exchange"},
    )
    assert "common/css/patient-facing.css" in html
    assert 'class="pf-page"' in html
    assert "PROBE-CONTENT" in html


def test_header_component_renders_brand_and_secure_marker():
    html = render_to_string(
        "common/patient_facing/components/header.html",
        {"brand_name": "JupyterHealth", "brand_logo": "common/images/jupyterhealth-logo.jpg"},
    )
    assert "JupyterHealth" in html
    assert "pf-header" in html
    assert "Secure" in html


def test_source_card_keeps_empty_badge_slot():
    html = render_to_string(
        "common/patient_facing/components/source_card.html",
        {"card": {"title": "Clinical records", "desc": "Labs, conditions, medications and more."}},
    )
    assert "Clinical records" in html
    assert "pf-card__badge" in html  # slot present in markup...
    # ...but empty (no status text), per the deferred connection-status decision (§6).
    assert "CONNECTED" not in html and "NOT CONNECTED" not in html


def test_config_context_brand_defaults(monkeypatch):
    from core.views import ehr_patient_portal as epp

    monkeypatch.setattr(epp, "_ehr_patient_portal_client", lambda: None)
    ctx = epp._config_context()
    assert ctx["brand_name"] == "JupyterHealth"
    assert ctx["brand_logo"] == "common/images/jupyterhealth-logo.jpg"


def test_config_context_brand_from_aux_data(monkeypatch):
    from core.views import ehr_patient_portal as epp

    class _Client:
        aux_data = {"brand_name": "Meridian Health", "brand_logo": "clients/ehr-patient-portal/images/meridian.svg"}

    class _App:
        jhe_client = _Client()
        data_sources = type("M", (), {"all": staticmethod(lambda: [])})()

    monkeypatch.setattr(epp, "_ehr_patient_portal_client", lambda: _App())
    ctx = epp._config_context()
    assert ctx["brand_name"] == "Meridian Health"
    assert ctx["brand_logo"] == "clients/ehr-patient-portal/images/meridian.svg"
