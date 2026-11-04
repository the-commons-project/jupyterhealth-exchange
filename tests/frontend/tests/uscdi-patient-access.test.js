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
    componentHtml(path.join(TEMPLATES, "clients/uscdi-patient-access/components/connect.html"));
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

describe("the shell in session mode", () => {
  test("sends no bearer token and the CSRF token on writes only", async () => {
    global.fetch = jest.fn(() => jsonResponse({}));
    await window.pfApi("GET", "users/profile");
    await window.pfApi("POST", "uscdi-patient-access/launch", { a: 1 });
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
  test("searches the brands API through the session", async () => {
    const rows = [{ id: 9, brandName: "Example Health", facilityName: "Clinic", addressText: "1 Main" }];
    global.fetch = jest.fn(() => jsonResponse({ results: rows }));
    expect(await window.upaSearchBrands("example")).toEqual(rows);
    expect(global.fetch.mock.calls[0][0]).toContain("/api/v1/ehr-patient-portal/brands?q=example");
  });

  test("a failed search is an empty list", async () => {
    global.fetch = jest.fn(() => jsonResponse({ error: "x" }, false));
    expect(await window.upaSearchBrands("x")).toEqual([]);
  });

  test("the launch posts the selection, the scopes and the data source", async () => {
    global.fetch = jest.fn(() => jsonResponse({ error: "stop before navigating" }, false));
    await expect(window.upaLaunch({ id: 9 }, ["patient/Observation.read"], "5")).rejects.toThrow();
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toContain("/api/v1/uscdi-patient-access/launch");
    expect(JSON.parse(opts.body)).toEqual({ locationId: 9, scopes: ["patient/Observation.read"], dataSourceId: 5 });
  });

  test("a vendor whose own screen asks per type skips the checklist and requests every type", async () => {
    global.PATIENT_FACING_CONFIG.typeChoiceByVendor = { "Epic Sandbox": "ehr", "SMART Sandbox": "host" };
    expect(window.upaTypeChoice({ vendorName: "Epic Sandbox" })).toBe("ehr");
    expect(window.upaTypeChoice({ vendorName: "SMART Sandbox" })).toBe("host");
    expect(window.upaTypeChoice({ vendorName: "Unknown" })).toBe("host");
    window.pfRender("t-connect", { rail: window.pfRail(1) });
    global.fetch = jest.fn(() => jsonResponse({ error: "stop before navigating" }, false));
    const row = { id: 9, vendorName: "Epic Sandbox", supportedScopes: "patient/Patient.read patient/Observation.read" };
    await window.upaAfterHospital(row, "5");
    expect(document.querySelectorAll("#scope-picker-list input[type=checkbox]").length).toBe(0);
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toContain("/api/v1/uscdi-patient-access/launch");
    expect(JSON.parse(opts.body).scopes).toEqual(["patient/Patient.read", "patient/Observation.read"]);
  });

  test("a vendor without its own per-type screen gets the checklist", async () => {
    global.PATIENT_FACING_CONFIG.typeChoiceByVendor = { "SMART Sandbox": "host" };
    window.pfRender("t-connect", { rail: window.pfRail(1) });
    global.fetch = jest.fn();
    await window.upaAfterHospital({ id: 9, vendorName: "SMART Sandbox", supportedScopes: "patient/Observation.read" }, "5");
    expect(document.getElementById("scope-picker").hidden).toBe(false);
    expect(document.querySelectorAll("#scope-picker-list input[type=checkbox]").length).toBe(1);
    expect(global.fetch).not.toHaveBeenCalled();
  });

  test("the checklist offers the vendor's scopes, all checked", () => {
    const list = document.createElement("div");
    const button = document.createElement("button");
    const chosen = jest.fn();
    window.upaRenderScopePicker(list, button, { supportedScopes: "patient/Observation.read patient/Unknown.read" }, chosen);
    expect(list.querySelectorAll("input[type=checkbox]").length).toBe(1);
    button.onclick();
    expect(chosen).toHaveBeenCalledWith(["patient/Observation.read"]);
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

  test("the status route goes to done when the job is done", async () => {
    global.fetch = jest.fn(() => jsonResponse(JOB));
    const nav = jest.spyOn(window, "pfNav").mockImplementation(() => Promise.resolve());
    await window.upaStatus({ job: "j1", source: "5" });
    expect(nav).toHaveBeenCalledWith("done", { source: "5", job: "j1" }, true);
    nav.mockRestore();
  });

  test("a failed job shows its reason", async () => {
    global.fetch = jest.fn(() =>
      jsonResponse({ ...JOB, status: "failed_terminal", failure: "Please connect again." })
    );
    await window.upaStatus({ job: "j1", source: "5" });
    expect(document.getElementById("pf_main").textContent).toContain("Please connect again.");
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

describe("hub and manage", () => {
  test("the hub card drops the record count JHE's store cannot know", () => {
    const source = { labels: ["Clinical records"] };
    expect(window.upaCardDesc(source, { ehrBrandLocationName: "Example Clinic", resourceCounts: {} })).toBe(
      "Example Clinic · Clinical records"
    );
    expect(window.upaCardDesc(source, null)).toBe("Clinical records");
  });

  test("manage shows what the latest delivered pull sent", async () => {
    document.body.innerHTML += componentHtml(path.join(TEMPLATES, "common/patient_facing/components/manage.html"));
    window.pfRegisterPartials();
    const source = { id: 5, name: "USCDI Patient Access", icon: "bi-x", consented: [{}], consentedLabels: ["Clinical records"] };
    const spy = jest.spyOn(window, "pfSource").mockImplementation(() => Promise.resolve(source));
    global.fetch = jest.fn(() => jsonResponse({ results: [{ ...JOB, status: "failed_terminal" }, JOB] }));
    await window.upaManage({ source: "5" });
    const text = document.getElementById("pf_main").textContent;
    expect(text).toContain("5 records sent");
    expect(global.fetch.mock.calls[0][0]).toContain("uscdi-patient-access/jobs?source=5");
    spy.mockRestore();
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
