import { describe, test, expect, beforeAll, beforeEach, jest } from "@jest/globals";

// client-patient-access.js exposes paPullResourceType + paWriteResource on window.
beforeAll(() => {
  require("../../../core/static/clients/patient-access/js/client-patient-access.js");
});

// Epic serves R4; JHE validates R5. Writes must go through the /fhir-import/R4/ endpoint,
// which converts R4->R5 and returns a batch-response Bundle whose single entry carries the
// real create status. A 200 HTTP response can still contain a per-entry 400.
function importResponse(entryStatus) {
  return Promise.resolve({
    ok: true,
    json: () => Promise.resolve({ resourceType: "Bundle", type: "batch-response", entry: [{ response: { status: entryStatus } }] }),
  });
}

beforeEach(() => {
  global.fetch = jest.fn(() => importResponse("201 Created"));
});

// patient.request = compartment searches; request = plain instance reads (used for Patient).
function fakeClient(items) {
  return {
    patient: { id: "epic-1", request: jest.fn(() => Promise.resolve(items)) },
    request: jest.fn(() => Promise.resolve(items)),
  };
}

const CONDITION_PULL = { label: "Conditions", type: "Condition", query: "Condition" };

describe("paWriteResource", () => {
  test("POSTs to the R4 import endpoint (not the R5 endpoint)", async () => {
    await window.paWriteResource("tok", "1", "Condition", { resourceType: "Condition" });
    const url = global.fetch.mock.calls[0][0];
    expect(url).toContain("/fhir-import/R4/Condition");
    expect(url).not.toContain("/FHIR/R5/");
  });

  test("returns true when the import entry status is 2xx", async () => {
    global.fetch = jest.fn(() => importResponse("201 Created"));
    expect(await window.paWriteResource("tok", "1", "Condition", {})).toBe(true);
  });

  test("returns false when the import entry status is 4xx (conversion/validation failed)", async () => {
    global.fetch = jest.fn(() => importResponse("400 invalid"));
    expect(await window.paWriteResource("tok", "1", "MedicationRequest", {})).toBe(false);
  });
});

describe("paPullResourceType", () => {
  test("writes each matching resource and counts them", async () => {
    const client = fakeClient([{ resourceType: "Condition" }, { resourceType: "Condition" }]);
    const r = await window.paPullResourceType(client, "tok", "1", CONDITION_PULL, "iss");
    expect(r).toEqual({ written: 2, failed: 0, error: null });
    expect(global.fetch).toHaveBeenCalledTimes(2);
  });

  test("skips resources of a different type", async () => {
    const client = fakeClient([{ resourceType: "Condition" }, { resourceType: "Observation" }]);
    const r = await window.paPullResourceType(client, "tok", "1", CONDITION_PULL, "iss");
    expect(r.written).toBe(1);
  });

  test("a pull failure is isolated, not thrown", async () => {
    const client = { patient: { id: "epic-1", request: jest.fn(() => Promise.reject(new Error("timeout"))) } };
    const r = await window.paPullResourceType(client, "tok", "1", CONDITION_PULL, "iss");
    expect(r).toEqual({ written: 0, failed: 0, error: "timeout" });
  });

  test("a per-entry import error counts as failed, not written", async () => {
    global.fetch = jest.fn(() => importResponse("400 invalid"));
    const client = fakeClient([{ resourceType: "Condition" }]);
    const r = await window.paPullResourceType(client, "tok", "1", CONDITION_PULL, "iss");
    expect(r).toEqual({ written: 0, failed: 1, error: null });
  });

  test("single read uses a plain instance read (not a patient-compartment search)", async () => {
    const client = fakeClient({ resourceType: "Patient", id: "epic-1" });
    const pull = { label: "Demographics", type: "Patient", query: "Patient", single: true };
    const r = await window.paPullResourceType(client, "tok", "1", pull, "iss");
    expect(r.written).toBe(1);
    expect(client.request).toHaveBeenCalledWith("Patient/epic-1");
    expect(client.patient.request).not.toHaveBeenCalled();
  });
});
