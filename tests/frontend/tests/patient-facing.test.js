import { describe, test, expect, beforeAll, beforeEach, jest } from "@jest/globals";
const fs = require("fs");
const path = require("path");

const STATIC = path.resolve(__dirname, "../../../core/static");
const COMPONENTS = path.resolve(__dirname, "../../../core/templates/common/patient_facing/components");

// A Django component file is a {% verbatim %}-wrapped <script type="text/template">; strip the tags to get the HTML.
function componentHtml(file) {
  return fs.readFileSync(file, "utf8").replace(/{% ?verbatim ?%}|{% ?endverbatim ?%}/g, "");
}

beforeAll(() => {
  global.Handlebars = require(path.join(STATIC, "common/js/handlebars.min.js"));
  require(path.join(STATIC, "common/js/common.js"));
  require(path.join(STATIC, "common/js/patient-facing.js"));
});

beforeEach(() => {
  global.PATIENT_PORTAL_CONFIG = { client: "ow", pageUrl: "/clients/ow/launch", siteTitle: "JupyterHealth Exchange", dataSourceIds: [3], sourceLabels: { 3: "Oura" }, expectedResourceTypes: [] };
  document.body.innerHTML = `<div id="pf_main"></div><div id="navLoadingOverlay" style="display:none"></div>` + componentHtml(path.join(COMPONENTS, "error.html"));
  window.sessionStorage.clear();
  global.fetch = jest.fn();
});

describe("pfRouteAndParams", () => {
  test("defaults to the hub and strips the route key from params", () => {
    expect(window.pfRouteAndParams("")).toEqual({ route: "hub", params: {} });
    expect(window.pfRouteAndParams("?route=consent&source=12")).toEqual({ route: "consent", params: { source: "12" } });
  });

  test("pfUrl round-trips route and params on the current page path", () => {
    window.history.replaceState({}, "", "/clients/ow/launch?route=hub");
    expect(window.pfUrl("manage", { source: "3" })).toBe("/clients/ow/launch?route=manage&source=3");
  });
});

describe("token helpers", () => {
  test("stores and reads the token from sessionStorage", () => {
    window.storeToken("tok");
    expect(window.getStoredToken()).toBe("tok");
    expect(window.sessionStorage.getItem("pf_access_token")).toBe("tok");
  });
});

describe("pfRedeemInvitation", () => {
  test("redeems the code, exchanges the grant and stores the token", async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ grant: { client_id: "cid", code: "authcode", redirect_uri: "http://x/cb" } }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ access_token: "jwt" }) });

    await window.pfRedeemInvitation("localhost%3A8001_abc");

    expect(window.getStoredToken()).toBe("jwt");
    expect(global.fetch.mock.calls[0][0]).toBe("http://localhost:8001/api/v1/invitation/abc");
    const tokenBody = global.fetch.mock.calls[1][1].body;
    expect(tokenBody).toContain("grant_type=authorization_code");
    expect(tokenBody).toContain("client_id=cid");
    expect(tokenBody).toContain("code_verifier=" + btoa("abc").replace(/=/g, ""));
  });

  test("throws the patient-readable message when the code is malformed or refused", async () => {
    await expect(window.pfRedeemInvitation("nounderscore")).rejects.toThrow("Ask your study team for a new link");
    global.fetch = jest.fn().mockResolvedValueOnce({ ok: false, status: 409 });
    await expect(window.pfRedeemInvitation("h_t")).rejects.toThrow("Ask your study team for a new link");
    expect(window.getStoredToken()).toBeNull();
  });
});

describe("pfApi", () => {
  test("adds the bearer token, JSON-encodes the body and parses the response", async () => {
    window.storeToken("tok");
    global.fetch = jest.fn().mockResolvedValue({ ok: true, status: 200, json: () => Promise.resolve({ id: 1 }) });

    const data = await window.pfApi("POST", "patients/7/consents", { study_scope_consents: [] });

    expect(data).toEqual({ id: 1 });
    const [url, opts] = global.fetch.mock.calls[0];
    expect(url).toBe("http://localhost/api/v1/patients/7/consents");
    expect(opts.method).toBe("POST");
    expect(opts.headers.Authorization).toBe("Bearer tok");
    expect(opts.headers["Content-Type"]).toBe("application/json");
    expect(JSON.parse(opts.body)).toEqual({ study_scope_consents: [] });
  });

  test("throws with the API's error text on a failed response", async () => {
    global.fetch = jest.fn().mockResolvedValue({ ok: false, status: 403, statusText: "Forbidden", json: () => Promise.resolve({ detail: "nope" }) });
    await expect(window.pfApi("GET", "users/profile")).rejects.toThrow("nope");
  });
});

describe("showFlowError and pfRender", () => {
  test("renders the error callout with Try again and Back on the client page", () => {
    window.showFlowError("We couldn't connect", "boom");
    const main = document.getElementById("pf_main");
    expect(main.querySelector(".pf-error__title").textContent).toBe("We couldn't connect");
    expect(main.querySelector(".pf-error__msg").textContent).toBe("boom");
    expect(main.querySelector("#pf_error_wrap .pf-actions")).not.toBeNull();
    expect(main.textContent).toContain("Try again");
    expect(main.querySelector(".pf-btn--ghost").getAttribute("href")).toBe("/clients/ow/launch");
  });

  test("uses a plain link for the retry when a retryHref is given", () => {
    window.showFlowError("t", "m", { retryLabel: "Choose a different organization", retryHref: "/clients/ehr-patient-portal/?route=connect&source=5" });
    const retry = document.querySelector("#pf_main .pf-btn");
    expect(retry.textContent).toBe("Choose a different organization");
    expect(retry.getAttribute("href")).toBe("/clients/ehr-patient-portal/?route=connect&source=5");
  });

  test("escapes context values", () => {
    window.pfRender("t-error", { title: "<b>x</b>", message: "" });
    expect(document.getElementById("pf_main").innerHTML).toContain("&lt;b&gt;x&lt;/b&gt;");
  });
});

describe("patientApp", () => {
  test("shows the invalid-invitation error when there is no code and no token", async () => {
    window.history.replaceState({}, "", "/clients/ow/launch");
    await window.patientApp();
    expect(document.querySelector(".pf-error__title").textContent).toBe("This invitation link isn't valid");
  });

  test("renders the error route from the query string", async () => {
    window.storeToken("tok");
    window.history.replaceState({}, "", "/clients/ow/launch?route=error&title=We+couldn%27t+connect+your+wearable&message=access_denied");
    await window.patientApp();
    expect(document.querySelector(".pf-error__title").textContent).toBe("We couldn't connect your wearable");
    expect(document.querySelector(".pf-error__msg").textContent).toBe("access_denied");
  });

  test("drops the code from the URL after redeeming and navigates to the route", async () => {
    global.fetch = jest
      .fn()
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ grant: { client_id: "c", code: "a", redirect_uri: "r" } }) })
      .mockResolvedValueOnce({ ok: true, json: () => Promise.resolve({ access_token: "jwt" }) });
    window.history.replaceState({}, "", "/clients/ow/launch?code=h_t&route=error&title=T&message=M");

    await window.patientApp();

    expect(window.location.search).toBe("?route=error&title=T&message=M");
    expect(document.querySelector(".pf-error__title").textContent).toBe("T");
  });
});
