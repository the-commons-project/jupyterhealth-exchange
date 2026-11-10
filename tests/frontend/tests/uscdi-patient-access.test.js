import { describe, test, expect, beforeAll, beforeEach, afterEach, jest } from "@jest/globals";
const fs = require("fs");
const path = require("path");

const STATIC = path.resolve(__dirname, "../../../core/static");
const TEMPLATES = path.resolve(__dirname, "../../../core/templates");

function componentHtml(file) {
  return fs.readFileSync(file, "utf8").replace(/{% ?verbatim ?%}|{% ?endverbatim ?%}/g, "");
}

function renderShell() {
  document.body.innerHTML =
    `<div id="pf_main"></div><div id="navLoadingOverlay"></div>` +
    ["rail", "error", "done", "receipt", "importing"]
      .map((c) => componentHtml(path.join(TEMPLATES, `common/patient_facing/components/${c}.html`)))
      .join("") +
    componentHtml(path.join(TEMPLATES, "clients/uscdi-patient-access/components/connect.html")) +
    componentHtml(path.join(TEMPLATES, "clients/uscdi-patient-access/components/manage.html"));
  window.pfRegisterPartials();
}

function jsonResponse(body, ok = true) {
  return Promise.resolve({ ok: ok, status: ok ? 200 : 400, json: () => Promise.resolve(body) });
}

function respond(status, body) {
  return Promise.resolve({ ok: status < 400, status: status, json: () => Promise.resolve(body) });
}

// Let the promise chains a click or a submit started run to the end.
function settle() {
  return new Promise((resolve) => setTimeout(resolve, 0));
}

const AUTHORIZE = "https://ehr.example.org/authorize?state=s";

function launchCalls() {
  return global.fetch.mock.calls.filter((c) => String(c[0]).endsWith("/api/v1/uscdi-patient-access/launch"));
}

const JOB = {
  id: "j1",
  status: "done",
  terminal: true,
  dataSourceId: 5,
  fhirSourceId: 90001,
  finishedAt: "2026-11-06T12:00:00Z",
  source: { brand: "Example Health", location: "Example Clinic" },
  types: [
    { label: "Labs", resourceType: "Observation", status: "ok", count: 3 },
    { label: "Vital Signs", resourceType: "Observation", status: "ok", count: 2 },
    { label: "Conditions", resourceType: "Condition", status: "not_requested", count: 0 },
  ],
  documents: { status: "ok", stored: 1, skipped: 0 },
  delivered: true,
  failure: null,
};

beforeAll(() => {
  global.Handlebars = require(path.join(STATIC, "common/js/handlebars.min.js"));
  require(path.join(STATIC, "common/js/common.js"));
  require(path.join(STATIC, "common/js/patient-facing.js"));
  require(path.join(STATIC, "clients/uscdi-patient-access/js/client-uscdi-patient-access.js"));
});

beforeEach(() => {
  global.fetch = jest.fn();
  sessionStorage.clear();
  global.PATIENT_FACING_CONFIG = {
    dataSourceIds: [5],
    sourceLabels: { 5: "USCDI Patient Access" },
    pageUrl: "/clients/uscdi-patient-access/",
    siteTitle: "T",
    expectedResourceTypes: ["Condition", "Observation"],
    available: true,
    allowHttpSignIn: false,
  };
  window.storeToken("tok");
  global.CONSTANTS = { EHR_SUPPORTED_SCOPES: { "patient/Observation.read": "Labs & Vital Signs" } };
  renderShell();
});

afterEach(() => {
  jest.restoreAllMocks();
});

// The connect step as the shell renders it. The launch answers with an authorize URL, and the page
// going there is captured instead of followed (jsdom cannot navigate).
async function connectStep(launchAnswer = () => respond(200, { authorizeUrl: AUTHORIZE })) {
  global.fetch = jest.fn((url) => (String(url).includes("/launch") ? launchAnswer() : jsonResponse({ results: [] })));
  await window.pfClient.connect({ id: 5 });
  const go = jest.spyOn(window.upaBrowser, "go").mockImplementation(() => {});
  return { form: document.getElementById("launch-form"), go };
}

function launched(i = 0) {
  const [, options] = launchCalls()[i];
  return { headers: options.headers, body: JSON.parse(options.body) };
}

describe("the picker and the launch", () => {
  test("searches the brands on the EHR Patient Portal's route, with the token", async () => {
    const rows = [{ id: 9, brandName: "Example Health", facilityName: "Clinic", addressText: "1 Main" }];
    global.fetch = jest.fn(() => jsonResponse({ results: rows }));
    expect(await window.upaSearchBrands("example")).toEqual(rows);
    expect(global.fetch.mock.calls[0][0]).toContain("/api/v1/ehr-patient-portal/brands?q=example");
    expect(global.fetch.mock.calls[0][1].headers.Authorization).toBe("Bearer tok");
  });

  test("a failed search is an empty list", async () => {
    global.fetch = jest.fn(() => jsonResponse({ error: "x" }, false));
    expect(await window.upaSearchBrands("x")).toEqual([]);
  });

  test("the connect form posts nowhere itself, and the search box stays outside it", async () => {
    const { form } = await connectStep();
    expect(form.getAttribute("method")).toBeNull();
    expect(form.getAttribute("action")).toBeNull();
    expect(new FormData(form).get("data_source_id")).toBe("5");
    expect(form.contains(document.getElementById("hospital-search"))).toBe(false);
    const submit = new Event("submit", { cancelable: true });
    form.dispatchEvent(submit);
    expect(submit.defaultPrevented).toBe(true); // never a native submit
  });

  test("the launch sends JSON with the token, then goes to the authorize URL", async () => {
    const { go } = await connectStep();
    document.getElementById("launch-location").value = "9";
    await window.upaLaunch(document.getElementById("launch-form"));
    const { headers, body } = launched();
    expect(headers.Authorization).toBe("Bearer tok");
    expect(headers["Content-Type"]).toBe("application/json");
    expect(body).toEqual({ location_id: "9", data_source_id: "5", scopes: [] });
    expect(go).toHaveBeenCalledWith(AUTHORIZE);
  });

  test("only an https authorize URL is followed, unless the server allows http", async () => {
    expect(window.upaSafeSignInUrl("https://ehr.example.org/a")).toBe(true);
    expect(window.upaSafeSignInUrl("http://ehr.example.org/a")).toBe(false);
    expect(window.upaSafeSignInUrl("javascript:alert(1)")).toBe(false);
    expect(window.upaSafeSignInUrl(undefined)).toBe(false);
    global.PATIENT_FACING_CONFIG.allowHttpSignIn = true;
    expect(window.upaSafeSignInUrl("http://localhost:9000/a")).toBe(true);
    global.PATIENT_FACING_CONFIG.allowHttpSignIn = false;
    const { go } = await connectStep(() => respond(200, { authorizeUrl: "http://ehr.example.org/a" }));
    await window.upaLaunch(document.getElementById("launch-form"));
    expect(go).not.toHaveBeenCalled();
    expect(document.getElementById("pf_main").textContent).toContain("We couldn't reach your healthcare organization");
  });

  test("a refused launch shows its reason's copy, never the server's text", async () => {
    const { go } = await connectStep(() => respond(400, { reason: "no_types", detail: "Server words" }));
    await window.upaLaunch(document.getElementById("launch-form"));
    const text = document.getElementById("pf_main").textContent;
    expect(text).toContain("Please choose at least one kind of record to share.");
    expect(text).not.toContain("Server words");
    expect(go).not.toHaveBeenCalled();
    expect(window.location.search).toContain("reason=no_types");
  });

  test("a server error at launch says sign-in is unavailable, not to pick again", async () => {
    await connectStep(() => Promise.resolve({ ok: false, status: 500, json: () => Promise.reject(new Error("html")) }));
    await window.upaLaunch(document.getElementById("launch-form"));
    expect(window.location.search).toContain("reason=sign_in_unavailable");
  });

  test("a launch whose token has died says the session has ended", async () => {
    await connectStep(() => respond(401, { detail: "Authentication credentials were not provided." }));
    await window.upaLaunch(document.getElementById("launch-form"));
    expect(document.getElementById("pf_main").textContent).toContain("Your session has ended");
  });

  test("a vendor whose own screen asks per type submits straight away, without the checklist", async () => {
    global.PATIENT_FACING_CONFIG.typeChoiceByVendor = { "Epic Sandbox": "ehr", "SMART Sandbox": "host" };
    expect(window.upaTypeChoice({ vendorName: "Epic Sandbox" })).toBe("ehr");
    expect(window.upaTypeChoice({ vendorName: "SMART Sandbox" })).toBe("host");
    expect(window.upaTypeChoice({ vendorName: "Unknown" })).toBe("host");
    const { go } = await connectStep();
    window.upaAfterHospital({ id: 9, vendorName: "Epic Sandbox", supportedScopes: "patient/Patient.read patient/Observation.read" });
    await settle();
    expect(launchCalls().length).toBe(1);
    expect(launched().body.location_id).toBe("9");
    expect(launched().body.scopes).toEqual([]); // the server requests every type for this vendor
    expect(document.getElementById("scope-picker").hidden).toBe(true);
    expect(go).toHaveBeenCalledWith(AUTHORIZE);
  });

  test("a vendor without its own per-type screen gets the checklist; Continue launches with the ticked types", async () => {
    global.PATIENT_FACING_CONFIG.typeChoiceByVendor = { "SMART Sandbox": "host" };
    global.CONSTANTS.EHR_SUPPORTED_SCOPES["patient/Patient.read"] = "Demographics";
    await connectStep();
    window.upaAfterHospital({ id: 9, vendorName: "SMART Sandbox", supportedScopes: "patient/Patient.read patient/Observation.read" });
    await settle();
    expect(launchCalls().length).toBe(0);
    expect(document.getElementById("scope-picker").hidden).toBe(false);
    document.querySelector('#scope-picker-list input[value="patient/Patient.read"]').checked = false;
    document.getElementById("scope-picker-continue").click();
    await settle();
    expect(launchCalls().length).toBe(1);
    expect(launched().body).toEqual({ location_id: "9", data_source_id: "5", scopes: ["patient/Observation.read"] });
  });

  test("the checklist offers the vendor's known scopes as ticked form fields", () => {
    const list = document.createElement("div");
    const rendered = window.upaRenderScopePicker(list, { supportedScopes: "patient/Observation.read patient/Unknown.read" });
    expect(rendered).toBe(1);
    const box = list.querySelector("input[type=checkbox]");
    expect([box.name, box.value, box.checked]).toEqual(["scopes", "patient/Observation.read", true]);
  });
});

describe("status and done", () => {
  test("per-type lines and progress copy", () => {
    expect(window.upaTypeLine(JOB.types[0])).toBe("Labs: 3 record(s)");
    expect(window.upaTypeLine(JOB.types[2])).toBe("Conditions: not selected");
    expect(window.upaProgressCopy({ ...JOB, status: "fetching" })).toBe("3 of 3 record types fetched · 5 records so far");
  });

  test("the receipt sums rows by resource type and lists what was not synced", () => {
    const receipt = window.upaReceipt(JOB);
    expect(receipt.synced).toEqual([{ label: expect.any(String), n: 5 }]);
    expect(receipt.notSynced.length).toBe(1);
    expect(receipt.total).toBe(5);
  });

  test("a type that was fetched and came back empty is synced with 0, not 'not synced'", () => {
    const job = { ...JOB, types: [...JOB.types, { label: "Immunizations", resourceType: "Immunization", status: "ok", count: 0 }] };
    global.PATIENT_FACING_CONFIG.expectedResourceTypes = ["Condition", "Observation", "Immunization"];
    const receipt = window.upaReceipt(job);
    expect(receipt.synced.map((r) => r.n)).toEqual([5, 0]);
    expect(receipt.notSynced.map((r) => r.n)).toEqual([0]); // Conditions: not requested
  });

  test("the status route goes to done when the job is done", async () => {
    global.fetch = jest.fn(() => jsonResponse(JOB));
    const nav = jest.spyOn(window, "pfNav").mockImplementation(() => Promise.resolve());
    await window.upaStatus({ job: "j1", source: "5" });
    expect(nav).toHaveBeenCalledWith("done", { source: "5", job: "j1" }, true);
    nav.mockRestore();
  });

  test("a failed job is worded by its kind, never with the server's own text", async () => {
    global.fetch = jest.fn(() =>
      jsonResponse({ ...JOB, status: "failed_terminal", terminal: true, failureKind: "reconnect", failure: "x" })
    );
    await window.upaStatus({ job: "j1", source: "5" });
    const text = document.getElementById("pf_main").textContent;
    expect(text).toContain("Please connect again");
    expect(text).toContain("Connect again");
  });

  test("every failure kind has patient copy, and an unknown one falls back", () => {
    for (const kind of ["retry_later", "reconnect", "contact_study", "consent_withdrawn"]) {
      const [title, message] = window.upaFailureCopy({ failureKind: kind });
      expect(title && message).toBeTruthy();
    }
    expect(window.upaFailureCopy({ failureKind: "new_kind" })[0]).toBe("We couldn't send your records");
  });

  test("the done screen reads the job, not JHE's FHIR store", async () => {
    global.fetch = jest.fn(() => jsonResponse(JOB));
    await window.upaDone({ job: "j1", source: "5" });
    const text = document.getElementById("pf_main").textContent;
    expect(text).toContain("Example Health");
    expect(text).toContain("5 records");
    expect(global.fetch.mock.calls[0][0]).toContain("uscdi-patient-access/jobs/j1");
  });
});

const CLINIC = { id: 90001, dataSource: 5, ehrBrandLocationName: "Example Clinic", label: "Example Health" };
const HOSPITAL = { id: 90002, dataSource: 5, ehrBrandLocationName: "Other Hospital", label: "Other" };

describe("hub and manage", () => {
  test("the hub card names one hospital and counts several, never the record count JHE's store cannot know", () => {
    const source = { id: 5, labels: ["Clinical records"] };
    expect(window.upaCardDesc(source, [CLINIC, { ...HOSPITAL, dataSource: 6 }])).toBe("Example Clinic · Clinical records");
    expect(window.upaCardDesc(source, [CLINIC, HOSPITAL])).toBe("2 healthcare organizations · Clinical records");
    expect(window.upaCardDesc(source, [])).toBe("Clinical records");
  });

  test("hospitals are grouped from the patient's sources with each one's latest delivered pull", () => {
    const older = { ...JOB, id: "j0", finishedAt: "2026-10-01T00:00:00Z", types: [{ label: "Labs", resourceType: "Observation", status: "ok", count: 1 }] };
    const jobs = [{ ...JOB, status: "failed_terminal", fhirSourceId: 90002 }, JOB, older];
    const rows = window.upaHospitals([HOSPITAL, CLINIC, { ...CLINIC, id: 7, dataSource: 6 }], jobs, 5);
    expect(rows.map((r) => r.name)).toEqual(["Example Clinic", "Other Hospital"]);
    expect(rows[0].detail).toMatch(/^5 records sent on /); // the newest done job, not the older one
    expect(rows[0].receipt.total).toBe(5);
    expect(rows[1].detail).toBe("Not yet synced");
    expect(rows[1].receipt).toBeNull();
  });

  test("manage lists every connected hospital and offers to connect another", async () => {
    const source = { id: 5, name: "USCDI Patient Access", icon: "bi-x", consented: [{}], consentedLabels: ["Clinical records"] };
    const spy = jest.spyOn(window, "pfSource").mockImplementation(() => Promise.resolve(source));
    global.fetch = jest.fn((url) =>
      String(url).includes("jobs") ? jsonResponse({ results: [JOB] }) : jsonResponse({ results: [CLINIC, HOSPITAL] })
    );
    await window.upaManage({ source: "5" });
    const text = document.getElementById("pf_main").textContent;
    expect(text).toContain("Example Clinic");
    expect(text).toContain("5 records sent");
    expect(text).toContain("Other Hospital");
    expect(text).toContain("Not yet synced");
    expect(text).toContain("Connect another organization");
    expect(text).toContain("Stop sharing");
    expect(global.fetch.mock.calls.some((c) => String(c[0]).includes("uscdi-patient-access/jobs?source=5"))).toBe(true);
    spy.mockRestore();
  });

  test("the hub counts a data source's hospitals on its card", async () => {
    document.body.innerHTML += componentHtml(path.join(TEMPLATES, "common/patient_facing/components/hub.html"));
    window.pfRegisterPartials();
    const source = { id: 5, name: "USCDI Patient Access", icon: "bi-x", isConsented: true, labels: ["Clinical records"], studies: ["S"] };
    const sources = jest.spyOn(window, "pfSourcesNow").mockImplementation(() => Promise.resolve([source]));
    const fhir = jest.spyOn(window, "pfFhirSources").mockImplementation(() => Promise.resolve([CLINIC, HOSPITAL]));
    await window.upaHub();
    expect(document.getElementById("pf_main").textContent).toContain("2 healthcare organizations · Clinical records");
    sources.mockRestore();
    fhir.mockRestore();
  });
});

describe("signing out", () => {
  test("hub, done and manage carry one Sign out button", () => {
    document.getElementById("pf_main").innerHTML = "<p>hub</p>";
    window.upaAppendSignOut();
    window.upaAppendSignOut(); // once only
    expect(document.querySelectorAll("#pf_main .upa-sign-out button").length).toBe(1);
  });

  test("Sign out revokes the token on the server, forgets it, and says so", async () => {
    global.fetch = jest.fn(() => respond(204, null));
    document.getElementById("pf_main").innerHTML = "<p>hub</p>";
    window.upaAppendSignOut();
    document.querySelector("#pf_main .upa-sign-out button").click();
    await settle();
    const [url, options] = global.fetch.mock.calls[0];
    expect(url).toContain("/api/v1/uscdi-patient-access/sign-out");
    expect(options.method).toBe("POST");
    expect(options.headers.Authorization).toBe("Bearer tok");
    expect(window.getStoredToken()).toBeNull();
    expect(document.getElementById("pf_main").textContent).toContain("You're signed out");
  });

  test("a revoke that fails still signs out here", async () => {
    global.fetch = jest.fn(() => Promise.reject(new Error("offline")));
    await window.upaSignOut();
    expect(window.getStoredToken()).toBeNull();
    expect(document.getElementById("pf_main").textContent).toContain("You're signed out");
  });
});

describe("the checklist", () => {
  test("Continue is disabled while nothing is ticked", async () => {
    global.PATIENT_FACING_CONFIG.typeChoiceByVendor = { "SMART Sandbox": "host" };
    await connectStep();
    window.upaAfterHospital({ id: 9, vendorName: "SMART Sandbox", supportedScopes: "patient/Observation.read" });
    const button = document.getElementById("scope-picker-continue");
    expect(button.disabled).toBe(false);
    const box = document.querySelector("#scope-picker-list input[name=scopes]");
    box.checked = false;
    box.dispatchEvent(new Event("change", { bubbles: true }));
    expect(button.disabled).toBe(true);
  });

  test("a vendor whose own screen asks says where the patient is going", async () => {
    global.PATIENT_FACING_CONFIG.typeChoiceByVendor = { "Epic Sandbox": "ehr" };
    await connectStep();
    window.upaAfterHospital({ id: 9, vendorName: "Epic Sandbox", brandName: "Epic Health", supportedScopes: "" });
    expect(document.getElementById("launch-status").textContent).toContain("Epic Health");
  });
});

describe("back from the EHR", () => {
  test("a page restored from the back-forward cache drops the overlay and restarts the connect step", async () => {
    await connectStep();
    window.history.replaceState({}, "", "/clients/uscdi-patient-access/?route=connect&source=5");
    window.pfShowLoading();
    const retry = jest.spyOn(window, "pfRetry").mockImplementation(() => {});
    const restored = new Event("pageshow");
    Object.defineProperty(restored, "persisted", { value: true });
    window.dispatchEvent(restored);
    expect(document.getElementById("navLoadingOverlay").style.display).toBe("none");
    expect(retry).toHaveBeenCalledTimes(1);
    retry.mockRestore();
  });

  test("an ordinary page load is left alone", () => {
    window.history.replaceState({}, "", "/clients/uscdi-patient-access/?route=connect&source=5");
    window.pfShowLoading();
    const retry = jest.spyOn(window, "pfRetry").mockImplementation(() => {});
    window.dispatchEvent(new Event("pageshow"));
    expect(document.getElementById("navLoadingOverlay").style.display).toBe("flex");
    expect(retry).not.toHaveBeenCalled();
    retry.mockRestore();
  });
});

// Every reason the server sends (Reason in core/views/uscdi_patient_access.py).
const SERVER_REASONS = [
  "unknown_selection",
  "not_configured",
  "sign_in_unavailable",
  "attempt_invalid",
  "declined",
  "no_code",
  "not_available",
  "not_a_patient",
  "invalid_request",
  "source_unavailable",
  "consent_required",
  "no_types",
  "browser_mismatch",
  "consent_withdrawn",
  "identifier_conflict",
];

function buttonLabels() {
  return [...document.querySelectorAll("#pf_main .pf-actions .pf-btn")].map((b) => b.textContent.trim());
}

describe("the error screen", () => {
  test("every reason the server sends has its own copy", () => {
    for (const reason of SERVER_REASONS) {
      expect(window.upaReasonCopy(reason)).toBe(window.UPA_REASON_COPY[reason]);
      expect(window.upaReasonCopy(reason).title).toBeTruthy();
    }
  });

  test("an unknown reason gets the generic copy, inherited names included", () => {
    for (const reason of ["new_reason", "constructor", "__proto__", undefined]) {
      expect(window.upaReasonCopy(reason).title).toBe("Something went wrong");
    }
  });

  test("only the reason is read: a title or message in the URL is never shown", () => {
    window.upaError({ reason: "declined", title: "Call 555-0100", message: "Their words" });
    const text = document.getElementById("pf_main").textContent;
    expect(text).toContain("Nothing was shared");
    expect(text).not.toContain("555-0100");
    expect(text).not.toContain("Their words");
  });

  test("nor in the tab title, which the shell would take from a URL title", async () => {
    await window.pfNav("error", { reason: "declined", title: "Call 555-0100" });
    expect(document.title).toBe("T - Nothing was shared");
  });

  test("a reason where retrying cannot help offers only Back", () => {
    window.upaError({ reason: "identifier_conflict" });
    expect(buttonLabels()).toEqual(["Back"]);
  });

  test("other reasons keep their retry", () => {
    window.upaError({ reason: "declined" });
    expect(buttonLabels().length).toBe(2);
  });
});

describe("the page's start", () => {
  function at(search) {
    window.history.replaceState({}, "", `/clients/uscdi-patient-access/${search}`);
  }

  test("with the module off, the page says so", () => {
    global.PATIENT_FACING_CONFIG.available = false;
    at("?route=hub");
    window.upaStart();
    expect(document.getElementById("pf_main").textContent).toContain("Not available");
  });

  test("an error the server redirected to shows in a tab without the token", () => {
    sessionStorage.clear();
    at("?route=error&reason=browser_mismatch");
    window.upaStart();
    const text = document.getElementById("pf_main").textContent;
    expect(text).toContain("Please connect from the same browser");
    expect(text).not.toContain("Your session has ended");
  });

  test("the status route in a tab without the token says the records are being sent", async () => {
    sessionStorage.clear();
    at("?route=status&job=j1&source=5");
    await window.upaStart();
    const text = document.getElementById("pf_main").textContent;
    expect(text).toContain("Your records are being sent to your study");
    expect(text).not.toContain("Your session has ended");
    expect(global.fetch).not.toHaveBeenCalled();
  });

  test("anywhere else, a tab without the token gets the shell's gate", async () => {
    sessionStorage.clear();
    at("?route=hub");
    await window.upaStart();
    expect(document.getElementById("pf_main").textContent).toContain("Your session has ended");
  });

  test("with the token, the shell runs the route", async () => {
    at("?route=hub");
    const hub = window.PF_ROUTES.hub;
    window.PF_ROUTES.hub = jest.fn();
    await window.upaStart();
    expect(window.PF_ROUTES.hub).toHaveBeenCalled();
    window.PF_ROUTES.hub = hub;
  });
});

