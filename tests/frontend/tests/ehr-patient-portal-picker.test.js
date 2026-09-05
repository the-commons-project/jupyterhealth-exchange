import { describe, test, expect, beforeAll, beforeEach, jest } from "@jest/globals";
const path = require("path");

const STATIC = path.resolve(__dirname, "../../../core/static");

// Loads patient-facing.js (router, pfRender, pfRail, ...) then client-ehr-patient-portal.js,
// which exposes the picker helpers and the connect step on window.
beforeAll(() => {
  global.Handlebars = require(path.join(STATIC, "common/js/handlebars.min.js"));
  require(path.join(STATIC, "common/js/common.js"));
  require(path.join(STATIC, "common/js/patient-facing.js"));
  require("../../../core/static/clients/ehr-patient-portal/js/client-ehr-patient-portal.js");
});

beforeEach(() => {
  global.fetch = jest.fn();
  delete global.FHIR;
  global.PATIENT_PORTAL_CONFIG = { clientId: "cid", scope: "launch/patient", dataSourceIds: [5], pageUrl: "/clients/ehr-patient-portal/", siteTitle: "T", expectedResourceTypes: [] };
});

describe("eppSearchBrands", () => {
  test("queries the brands API with the JHE token and returns the results", async () => {
    const rows = [{ brandName: "Mount Sinai", facilityName: "MSH", fhirBaseUrl: "https://s/FHIR/R4", addressText: "NY" }];
    global.fetch = jest.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ results: rows }) }));

    const out = await window.eppSearchBrands("tok", "sinai");

    expect(out).toEqual(rows);
    const [url, opts] = global.fetch.mock.calls[0];
    // Hyphen, matching the route in core/urls.py. An underscore 404s and eppSearchBrands
    // swallows it into [], so the picker just says "no hospitals found".
    expect(url).toContain("/api/v1/ehr-patient-portal/brands");
    expect(url).toContain("q=sinai");
    expect(opts.headers.Authorization).toBe("Bearer tok");
  });

  test("returns [] on a failed response", async () => {
    global.fetch = jest.fn(() => Promise.resolve({ ok: false }));
    const out = await window.eppSearchBrands("tok", "x");
    expect(out).toEqual([]);
  });
});

describe("eppAuthorizeWithIss", () => {
  test("launches SMART authorize with the selected hospital's iss", () => {
    const authorize = jest.fn();
    global.FHIR = { oauth2: { authorize } };
    const config = { clientId: "cid", scope: "launch/patient" };

    window.eppAuthorizeWithIss(config, "https://sinai/FHIR/R4");

    expect(authorize).toHaveBeenCalledTimes(1);
    const arg = authorize.mock.calls[0][0];
    expect(arg.iss).toBe("https://sinai/FHIR/R4");
    expect(arg.clientId).toBe("cid");
    expect(arg.scope).toBe("launch/patient");
    expect(arg.redirectUri).toContain("/clients/ehr-patient-portal/callback");
  });
});

describe("eppRenderBrandResults", () => {
  test("renders a clickable row per result and fires onSelect with that result", () => {
    const container = document.createElement("div");
    const rows = [
      { brandName: "Mount Sinai", facilityName: "MSH", fhirBaseUrl: "https://a/FHIR/R4", addressText: "1 Levy Pl" },
      { brandName: "Mercy", facilityName: "Mercy STL", fhirBaseUrl: "https://b/FHIR/R4", addressText: "MO" },
    ];
    const onSelect = jest.fn();

    const n = window.eppRenderBrandResults(container, rows, onSelect);

    expect(n).toBe(2);
    const items = container.querySelectorAll("[data-brand-result]");
    expect(items.length).toBe(2);
    // the row shows the hospital name + address to the patient
    expect(items[0].textContent).toContain("Mount Sinai");
    expect(items[0].textContent).toContain("1 Levy Pl");
    items[1].click();
    expect(onSelect).toHaveBeenCalledWith(rows[1]);
  });

  test("shows a no-results message when empty", () => {
    const container = document.createElement("div");
    const n = window.eppRenderBrandResults(container, [], jest.fn());
    expect(n).toBe(0);
    expect(container.textContent.toLowerCase()).toContain("no ");
  });
});

describe("eppImportFailure", () => {
  test("is the last Error line, or the all-types-failed message, or null", () => {
    expect(window.eppImportFailure("Fetching Labs\n  could not fetch Labs: 403\nFetching Vitals\n  could not fetch Vitals: 403")).toBe("none of your record types could be fetched");
    expect(window.eppImportFailure("Fetching Labs\n  saved 3 record(s)\n  could not fetch Vitals: 403")).toBeNull();
    expect(window.eppImportFailure("Completing connection...\nError: no JHE session. Restart from your invitation link.")).toBe("no JHE session. Restart from your invitation link.");
  });
});

describe("pfClient.connect on the EHR page", () => {
  test("renders the picker at rail step 1 and searches brands as the patient types", async () => {
    const fs = require("fs");
    const path = require("path");
    const strip = (file) => fs.readFileSync(file, "utf8").replace(/{% ?verbatim ?%}|{% ?endverbatim ?%}/g, "");
    const templates = path.resolve(__dirname, "../../../core/templates");
    document.body.innerHTML = `<div id="pf_main"></div>` + strip(path.join(templates, "common/patient_facing/components/rail.html")) + strip(path.join(templates, "clients/ehr-patient-portal/components/connect.html"));
    window.pfRegisterPartials();
    window.storeToken("tok");
    global.fetch = jest.fn(() => Promise.resolve({ ok: true, json: () => Promise.resolve({ results: [{ id: 9, brandName: "Epic Sandbox", facilityName: "Madison", fhirBaseUrl: "https://epic/FHIR/R4", addressText: "WI" }] }) }));

    await window.pfClient.connect({ id: 5, name: "EHR Patient Portal" });

    const main = document.getElementById("pf_main");
    expect(main.querySelector(".pf-rail__step").className).toBe("pf-rail__step is-active");
    expect(main.querySelector(".pf-h1").textContent).toBe("Share your medical records");
    expect(main.querySelector("#hospital-picker").hidden).toBe(false);
    expect(main.querySelectorAll("#hospital-results [data-brand-result]")).toHaveLength(1);
    const authorize = jest.fn();
    global.FHIR = { oauth2: { authorize } };
    main.querySelector("#hospital-results [data-brand-result]").click();
    expect(authorize).toHaveBeenCalledWith(expect.objectContaining({ iss: "https://epic/FHIR/R4", clientId: "cid" }));
    expect(window.sessionStorage.getItem("ehr_patient_portal_brand_location_id")).toBe("9");
  });
});
