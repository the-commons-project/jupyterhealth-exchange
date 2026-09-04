import re
from pathlib import Path

from django.conf import settings
from django.template.loader import render_to_string
from django.test import Client

STATIC = Path(settings.BASE_DIR) / "core" / "static"


def _rail_step_classes(html):
    return re.findall(r'<li class="pf-rail__step([^"]*)"', html)


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
                ".pf-card", ".pf-card__badge", ".pf-log",
                ".pf-rail", ".pf-callout", "#hospital-results"):
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


def test_progress_rail_marks_active_and_done_steps():
    html = render_to_string(
        "common/patient_facing/components/progress_rail.html",
        {"active_step": 2},
    )
    assert "pf-rail" in html
    steps = _rail_step_classes(html)
    assert len(steps) == 3
    assert "is-done" in steps[0] and "is-active" not in steps[0]
    assert "is-active" in steps[1] and "is-done" not in steps[1]
    assert "is-done" not in steps[2] and "is-active" not in steps[2]
    assert "Choose organization" in html
    assert "Sign in" in html
    assert "Import records" in html


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


def test_connect_page_is_branded_and_preserves_js_hooks(db):
    resp = Client().get("/clients/ehr-patient-portal/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "common/css/patient-facing.css" in html      # branded base applied
    assert 'class="pf-page"' in html                     # page shell
    assert 'id="hospital-picker"' in html                # JS/test hooks preserved
    assert 'id="hospital-search"' in html
    assert 'id="hospital-results"' in html
    assert '<pre id="out" class="pf-log" hidden>' in html  # kept for the flow JS, not shown
    assert "EHR_PATIENT_PORTAL_CONFIG" in html           # flow config global intact
    assert "startEhrPatientPortalConnect" in html        # flow entrypoint intact
    assert "Share your medical records" in html          # pe-5 headline
    assert "pf-rail" in html                              # progress rail present
    steps = _rail_step_classes(html)
    assert len(steps) == 3 and "is-active" in steps[0]    # step 1 active on connect
    assert "We only copy the records you approve" in html  # info callout copy
    assert "sign in with them directly" in html            # new lede copy


def test_callback_page_frames_output_and_preserves_flow(db):
    resp = Client().get("/clients/ehr-patient-portal/callback")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'class="pf-page"' in html
    assert 'id="out"' in html and "pf-log" in html      # output framed, not raw
    assert "finishEhrPatientPortalConnect" in html       # flow entrypoint intact
    assert "Importing your records" in html              # pe-6 headline
    assert "pf-rail" in html                              # progress rail present
    steps = _rail_step_classes(html)
    assert len(steps) == 3 and "is-active" in steps[2]    # step 3 active on callback


def test_invitation_email_is_branded_and_typo_free():
    html = render_to_string(
        "registration/invitation_email.html",
        {"patient_name": "Maria", "invitation_link": "https://jhe.example/redeem?code=abc"},
    )
    assert "JupyterHeath" not in html                  # typo fixed
    assert "JupyterHealth" in html                     # correct brand
    assert "Maria" in html                             # greeting preserved
    assert "https://jhe.example/redeem?code=abc" in html  # link preserved
    assert "Get started" in html or "GET STARTED" in html # pe-1 CTA
    assert "style=" in html                            # inline-styled for email clients
