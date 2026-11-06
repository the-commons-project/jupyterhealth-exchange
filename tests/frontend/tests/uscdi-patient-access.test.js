import { describe, test, expect, beforeAll, beforeEach, jest } from "@jest/globals";
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
    auth: "session",
    csrfToken: "csrf-1",
    launchUrl: "/clients/uscdi-patient-access/launch",
    dataSourceIds: [5],
    sourceLabels: { 5: "USCDI Patient Access" },
    pageUrl: "/clients/uscdi-patient-access/",
    siteTitle: "T",
    expectedResourceTypes: ["Condition", "Observation"],
    flowError: null,
  };
  global.CONSTANTS = { EHR_SUPPORTED_SCOPES: { "patient/Observation.read": "Labs & Vital Signs" } };
  renderShell();
});

// The connect step as the shell renders it, with the launch form's submissions captured instead of
// followed (jsdom cannot navigate): `posted` collects each submission's fields.
async function connectStep() {
  global.fetch = jest.fn(() => jsonResponse({ results: [] }));
  await window.pfClient.connect({ id: 5 });
  const form = document.getElementById("launch-form");
  const posted = [];
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    posted.push(new FormData(form));
  });
  return { form, posted };
}

describe("the shell in session mode", () => {
  test("sends no bearer token and the CSRF token on writes only", async () => {
    global.fetch = jest.fn(() => jsonResponse({}));
    await window.pfApi("GET", "users/profile");
    await window.pfApi("POST", "patients/1/consents", { a: 1 });
    const [, getOpts] = global.fetch.mock.calls[0];
    const [, postOpts] = global.fetch.mock.calls[1];
    expect(getOpts.headers.Authorization).toBeUndefined();
    expect(getOpts.headers["X-CSRFToken"]).toBeUndefined();
    expect(postOpts.headers["X-CSRFToken"]).toBe("csrf-1");
    expect(postOpts.credentials).toBe("same-origin");
  });

  test("other clients keep their bearer token", async () => {
    global.PATIENT_FACING_CONFIG.auth = undefined;
    window.storeToken("tok");
    global.fetch = jest.fn(() => jsonResponse({}));
    await window.pfApi("PATCH", "patients/1/consents", {});
    const [, opts] = global.fetch.mock.calls[0];
    expect(opts.headers.Authorization).toBe("Bearer tok");
    expect(opts.headers["X-CSRFToken"]).toBeUndefined();
  });

  test("the stored-token gate does not apply", async () => {
    window.PF_ROUTES.hub = jest.fn();
    await window.patientApp();
    expect(document.getElementById("pf_main").textContent).not.toContain("Your session has ended");
    expect(window.PF_ROUTES.hub).toHaveBeenCalled();
  });
});

describe("the picker and the launch", () => {
  test("searches the brands through the client's own route", async () => {
    const rows = [{ id: 9, brandName: "Example Health", facilityName: "Clinic", addressText: "1 Main" }];
    global.fetch = jest.fn(() => jsonResponse({ results: rows }));
    expect(await window.upaSearchBrands("example")).toEqual(rows);
    expect(global.fetch.mock.calls[0][0]).toContain("/api/v1/uscdi-patient-access/brands?q=example");
  });

  test("a failed search is an empty list", async () => {
    global.fetch = jest.fn(() => jsonResponse({ error: "x" }, false));
    expect(await window.upaSearchBrands("x")).toEqual([]);
  });

  test("the connect step is a real form posting to the launch, with the search box outside it", async () => {
    const { form } = await connectStep();
    expect(form.getAttribute("method")).toBe("post");
    expect(form.getAttribute("action")).toBe("/clients/uscdi-patient-access/launch");
    const fields = new FormData(form);
    expect(fields.get("csrfmiddlewaretoken")).toBe("csrf-1");
    expect(fields.get("data_source_id")).toBe("5");
    expect(form.contains(document.getElementById("hospital-search"))).toBe(false);
  });

  test("a vendor whose own screen asks per type submits straight away, without the checklist", async () => {
    global.PATIENT_FACING_CONFIG.typeChoiceByVendor = { "Epic Sandbox": "ehr", "SMART Sandbox": "host" };
    expect(window.upaTypeChoice({ vendorName: "Epic Sandbox" })).toBe("ehr");
    expect(window.upaTypeChoice({ vendorName: "SMART Sandbox" })).toBe("host");
    expect(window.upaTypeChoice({ vendorName: "Unknown" })).toBe("host");
    const { posted } = await connectStep();
    window.upaAfterHospital({ id: 9, vendorName: "Epic Sandbox", supportedScopes: "patient/Patient.read patient/Observation.read" });
    expect(posted.length).toBe(1);
    expect(posted[0].get("location_id")).toBe("9");
    expect(posted[0].getAll("scopes")).toEqual([]); // the server requests every type for this vendor
    expect(document.getElementById("scope-picker").hidden).toBe(true);
  });

  test("a vendor without its own per-type screen gets the checklist; Continue posts the ticked types", async () => {
    global.PATIENT_FACING_CONFIG.typeChoiceByVendor = { "SMART Sandbox": "host" };
    global.CONSTANTS.EHR_SUPPORTED_SCOPES["patient/Patient.read"] = "Demographics";
    const { posted } = await connectStep();
    window.upaAfterHospital({ id: 9, vendorName: "SMART Sandbox", supportedScopes: "patient/Patient.read patient/Observation.read" });
    expect(posted.length).toBe(0);
    expect(document.getElementById("scope-picker").hidden).toBe(false);
    document.querySelector('#scope-picker-list input[value="patient/Patient.read"]').checked = false;
    document.getElementById("scope-picker-continue").click();
    expect(posted.length).toBe(1);
    expect(posted[0].get("location_id")).toBe("9");
    expect(posted[0].getAll("scopes")).toEqual(["patient/Observation.read"]);
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

describe("errors carried by the server", () => {
  test("a carried error is shown instead of the app", () => {
    global.PATIENT_FACING_CONFIG.flowError = { title: "This invitation link isn't valid", message: "Ask for a new link." };
    window.upaStart();
    const text = document.getElementById("pf_main").textContent;
    expect(text).toContain("This invitation link isn't valid");
    expect(text).toContain("Ask for a new link.");
  });

  test("without one, the error route falls back to the query string", () => {
    window.upaError({ title: "Something", message: "From the URL" });
    expect(document.getElementById("pf_main").textContent).toContain("From the URL");
  });
});

describe("the shell's calls and signing out", () => {
  test("the shell's own calls go to the client's routes when it maps them", async () => {
    global.PATIENT_FACING_CONFIG.apiPaths = { profile: "uscdi-patient-access/profile" };
    expect(window.pfPath("profile", "users/profile")).toBe("uscdi-patient-access/profile");
    expect(window.pfPath("fhirSources", "fhir_sources")).toBe("fhir_sources");
    global.PATIENT_FACING_CONFIG.apiPaths = undefined;
    expect(window.pfPath("profile", "users/profile")).toBe("users/profile"); // every other client
  });

  test("hub, done and manage carry a sign-out form posting with the CSRF token", () => {
    global.PATIENT_FACING_CONFIG.signOutUrl = "/clients/uscdi-patient-access/sign-out";
    document.getElementById("pf_main").innerHTML = "<p>hub</p>";
    window.upaAppendSignOut();
    window.upaAppendSignOut(); // once only
    const forms = document.querySelectorAll("#pf_main form.upa-sign-out");
    expect(forms.length).toBe(1);
    expect(forms[0].getAttribute("action")).toBe("/clients/uscdi-patient-access/sign-out");
    expect(new FormData(forms[0]).get("csrfmiddlewaretoken")).toBe("csrf-1");
  });

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

describe("the error screen", () => {
  test("a message where retrying cannot help offers only Back", () => {
    global.PATIENT_FACING_CONFIG.flowError = { title: "You're signed out", message: "Close this page.", retry: false };
    window.upaError({});
    const labels = [...document.querySelectorAll("#pf_main .pf-actions .pf-btn")].map((b) => b.textContent.trim());
    expect(labels).toEqual(["Back"]);
  });

  test("other errors keep their retry", () => {
    global.PATIENT_FACING_CONFIG.flowError = { title: "We couldn't start the connection", message: "x", retry: true };
    window.upaError({});
    expect(document.querySelectorAll("#pf_main .pf-actions .pf-btn").length).toBe(2);
  });
});

