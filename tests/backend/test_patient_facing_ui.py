import io
import re
from pathlib import Path

from django.conf import settings
from django.core.management import call_command
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
                ".pf-rail", ".pf-callout", "#hospital-results",
                ".pf-card__icon", ".pf-actions", ".pf-back"):
        assert cls in css, f"missing component class {cls}"
    # Status log stays visible until the hospital picker reveals (i.e. only hides on success).
    assert "#hospital-picker:not([hidden]) ~ #out" in css
    # The lockup logo renders at legible size, no box/border/background around it.
    assert "height: 32px" in css.split(".pf-header__logo", 1)[1].split("}", 1)[0]


def test_patient_facing_css_hides_results_until_typing():
    css = (STATIC / "common" / "css" / "patient-facing.css").read_text()
    # The hospital results list only appears once the patient has typed something.
    assert "#hospital-search:placeholder-shown ~ #hospital-results" in css


def test_patient_facing_css_hides_import_log_behind_progress_card():
    css = (STATIC / "common" / "css" / "patient-facing.css").read_text()
    # The raw flow-JS log stays in the DOM (JS still writes to it) but is visually replaced
    # by the branded importing card, scoped so it doesn't affect #out on other pages.
    assert ".pf-import #out" in css
    assert ".pf-progress__bar" in css
    assert "@keyframes" in css


def test_patient_facing_css_styles_back_as_a_ghost_button():
    css = (STATIC / "common" / "css" / "patient-facing.css").read_text()
    back_rule = css.split(".pf-back {", 1)[1].split("}", 1)[0]
    assert "border: 1px solid var(--pf-line)" in back_rule
    assert "border-radius: var(--pf-radius-lg)" in back_rule
    assert "text-transform: uppercase" in back_rule
    assert "padding: 10px 16px" in back_rule
    # The check circle it sits above must be block-level (not inline-flex) so it always starts
    # on its own line below the back button instead of crowding onto the same row (§H).
    check_circle_rule = css.split(".pf-check-circle {", 1)[1].split("}", 1)[0]
    assert "display: flex" in check_circle_rule
    assert "inline-flex" not in check_circle_rule


def test_patient_facing_css_defines_receipt_component():
    css = (STATIC / "common" / "css" / "patient-facing.css").read_text()
    for cls in (".pf-receipt", ".pf-receipt__row", ".pf-receipt__n", ".pf-receipt__heading"):
        assert cls in css, f"missing component class {cls}"


def test_patient_facing_base_links_stylesheet_and_wraps_page():
    html = render_to_string(
        "common/patient_facing/_test_probe.html",
        {"SITE_TITLE": "JupyterHealth Exchange"},
    )
    assert "common/css/patient-facing.css" in html
    assert 'class="pf-page"' in html
    assert "PROBE-CONTENT" in html


def test_base_template_cache_busts_the_stylesheet():
    from core.context_processors import PF_CSS_VERSION

    html = render_to_string(
        "common/patient_facing/_test_probe.html",
        {"SITE_TITLE": "JupyterHealth Exchange", "PF_CSS_VERSION": PF_CSS_VERSION},
    )
    assert re.search(r"patient-facing\.css\?v=\d+", html)


def test_header_component_renders_brand_and_secure_marker():
    html = render_to_string(
        "common/patient_facing/components/header.html",
        {"brand_name": "JupyterHealth", "brand_logo": "common/images/jupyterhealth-logo.jpg"},
    )
    assert "JupyterHealth" in html
    assert "pf-header" in html
    assert "Secure" in html


def test_header_component_hides_brand_name_by_default_shows_it_when_flagged():
    default_html = render_to_string(
        "common/patient_facing/components/header.html",
        {"brand_name": "JupyterHealth", "brand_logo": "common/images/jupyterhealth-logo.png"},
    )
    # The lockup already carries the name, so no adjacent brand-name text renders by default
    # -- only the <img alt> attribute mentions it, not any visible text alongside it.
    brand_span = re.sub(r"<img[^>]*>", "", default_html.split("pf-header__brand", 1)[1])
    assert "JupyterHealth" not in brand_span

    rebrand_html = render_to_string(
        "common/patient_facing/components/header.html",
        {
            "brand_name": "Meridian Health",
            "brand_logo": "clients/ehr-patient-portal/images/meridian.svg",
            "brand_name_visible": True,
        },
    )
    # A rebrand's own mark is not a lockup, so the name renders alongside it.
    assert "Meridian Health" in rebrand_html.split("pf-header__secure", 1)[0]


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
    assert ctx["brand_logo"] == "common/images/jupyterhealth-logo.png"
    assert ctx["brand_name_visible"] is False


def test_default_logo_is_the_full_lockup(monkeypatch):
    from core.views import ehr_patient_portal as epp

    monkeypatch.setattr(epp, "_ehr_patient_portal_client", lambda: None)
    ctx = epp._config_context()
    assert ctx["brand_logo"] == "common/images/jupyterhealth-logo.png"
    logo = STATIC / "common" / "images" / "jupyterhealth-logo.png"
    assert logo.exists()
    assert logo.stat().st_size > 5000
    # The square mark file stays vendored in place too, even though nothing defaults to it now.
    mark = STATIC / "common" / "images" / "jupyterhealth-mark.png"
    assert mark.exists()


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
    assert ctx["brand_name_visible"] is True


def test_connect_page_is_branded_and_preserves_js_hooks(db):
    resp = Client().get("/clients/ehr-patient-portal/")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert "common/css/patient-facing.css" in html      # branded base applied
    assert 'class="pf-page"' in html                     # page shell
    assert 'id="hospital-picker"' in html                # JS/test hooks preserved
    assert 'id="hospital-search"' in html
    assert 'id="hospital-results"' in html
    assert '<pre id="out" class="pf-log">' in html        # visible by default -- failures show up
    assert html.index('id="hospital-picker"') < html.index('id="out"')  # #out follows the picker in DOM order
    # The results list now lives inside the search wrapper, right after the input (§F) --
    # DOM position is free since the flow JS looks these up by id, not by nesting.
    search_wrapper = html.split('class="pf-search"', 1)[1]
    assert search_wrapper.index('id="hospital-search"') < search_wrapper.index('id="hospital-results"')
    assert "EHR_PATIENT_PORTAL_CONFIG" in html           # flow config global intact
    assert "startEhrPatientPortalConnect" in html        # flow entrypoint intact
    assert "Share your medical records" in html          # pe-5 headline
    assert "pf-rail" in html                              # progress rail present
    steps = _rail_step_classes(html)
    assert len(steps) == 3 and "is-active" in steps[0]    # step 1 active on connect
    assert "We only sync the records you approve" in html  # info callout copy
    assert "sign in with them directly" in html            # new lede copy
    assert "pf-back" in html and 'href="/patient/"' in html  # back link to the hub (§H)


def test_callback_page_frames_output_and_preserves_flow(db):
    resp = Client().get("/clients/ehr-patient-portal/callback")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'class="pf-page"' in html
    assert 'id="out"' in html and "pf-log" in html      # the flow JS's log stays in the DOM...
    assert "finishEhrPatientPortalConnect" in html       # flow entrypoint intact
    assert "Importing your records" in html              # pe-6 headline
    assert "Securely syncing your records into your study" in html  # sync-wording lede (copy amendment)
    assert "pf-import-card" in html and "pf-progress" in html  # ...but is visually replaced by the progress card (§I)
    assert "Syncing your records" in html                 # sync-wording card title
    assert "pf-rail" in html                              # progress rail present
    steps = _rail_step_classes(html)
    assert len(steps) == 3 and "is-active" in steps[2]    # step 3 active on callback
    assert 'href="/patient/done/"' in html and "View summary" in html  # pe-7 link (Task 15)
    assert "When the sync finishes, view your summary." in html  # guards mid-import taps, log is hidden now
    assert "pf-back" in html and 'href="/patient/"' in html  # back link to the hub (§H)


def test_ow_launch_is_branded_and_preserves_flow(db):
    resp = Client().get("/clients/ow/launch")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'class="pf-page"' in html                      # branded base applied
    assert "clients/ow/js/client-ow.js" in html            # flow JS loaded
    assert "Connect your" in html                          # pe-4 headline
    assert 'id="out"' in html
    assert 'id="consent_form"' in html
    assert "pf-back" in html and 'href="/patient/"' in html  # back link to the hub (§H)
    for entrypoint in (
        "run(",
        "parseInvitationCode(",
        "renderConsentForm(",
        "submitConsents(",
        "continueOwFlow(",
        "getOuraAuthUrl(",
    ):
        assert entrypoint in html, f"missing flow entrypoint {entrypoint}"

    call_command("seed", stdout=io.StringIO())
    resp = Client().get("/clients/ow/launch")
    assert "Connect your Oura" in resp.content.decode()    # seeded OW DataSource name


def test_ow_manage_and_complete_render_on_branded_base(db):
    resp = Client().get("/clients/ow/manage")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'class="pf-page"' in html
    assert 'id="consent_form"' in html
    assert 'id="status_badge"' in html

    resp = Client().get("/clients/ow/complete")
    assert resp.status_code == 200
    html = resp.content.decode()
    assert 'class="pf-page"' in html
    assert 'id="out"' in html
    assert 'id="manage_link"' in html


def test_invitation_email_is_branded_and_typo_free():
    html = render_to_string(
        "registration/invitation_email.html",
        {
            "patient_name": "Maria",
            "invitation_link": "https://jhe.example/redeem?code=abc",
            "site_url": "https://jhe.example",
        },
    )
    assert "JupyterHeath" not in html                  # typo fixed
    assert "JupyterHealth" in html                     # correct brand
    assert "Maria" in html                             # greeting preserved
    assert "https://jhe.example/redeem?code=abc" in html  # link preserved
    assert "Get started" in html or "GET STARTED" in html # pe-1 CTA
    assert "style=" in html                            # inline-styled for email clients


def test_invitation_email_footer_mark_and_study():
    html = render_to_string(
        "registration/invitation_email.html",
        {
            "patient_name": "Maria",
            "invitation_link": "https://jhe.example/r?code=abc",
            "site_url": "https://jhe.example",
            "study_name": "Cardiometabolic Health Study",
        },
    )
    assert "You received this because a study team invited you" in html
    assert "https://jhe.example/static/common/images/jupyterhealth-logo.png" in html  # full lockup, not the mark
    assert "<strong>Cardiometabolic Health Study</strong>" in html

    html_no_study = render_to_string(
        "registration/invitation_email.html",
        {
            "patient_name": "Maria",
            "invitation_link": "https://jhe.example/r?code=abc",
            "site_url": "https://jhe.example",
        },
    )
    assert "invited to securely share" in html_no_study
    assert "<strong>" not in html_no_study
