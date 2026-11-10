// ────────────────────────────────────────────────────
// USCDI Patient Access client: the patient's records pulled from their hospital's patient portal
// on the server and delivered to the study's storage. Registers, for the shared patient-facing
// app (core/static/common/js/patient-facing.js):
//   - the connect step: hospital picker, then "choose what to share" (copied from the EHR Patient
//     Portal client, not shared with it), then the server's launch, which answers with the EHR's
//     sign-in URL and sets this browser's callback cookie;
//   - the status route the server's callback redirects to, polling the job;
//   - its own done and error screens (the done screen reads the job, not JHE's FHIR store, which
//     this client never writes to; the error screen words a reason code, never text from the URL).
// Authentication is the shell's, as for every patient client: the invitation link is redeemed for
// a bearer token kept in sessionStorage.
// ────────────────────────────────────────────────────

const UPA_POLL_MS = 2000;

// Search hospital facilities for the picker. Returns an array of rows (or [] on any failure).
async function upaSearchBrands(query) {
  try {
    const out = await pfApi("GET", `ehr-patient-portal/brands?q=${encodeURIComponent(query || "")}`);
    return (out && out.results) || [];
  } catch (e) {
    return [];
  }
}

// One pre-checked row per scope the vendor supports (row.supportedScopes), labelled from
// CONSTANTS.EHR_SUPPORTED_SCOPES. Each checkbox is a form field (name "scopes"), so the ticked
// ones are what the launch sends. Returns how many rows were rendered.
function upaRenderScopePicker(container, row) {
  container.innerHTML = "";
  const supported = (row.supportedScopes || "").split(/\s+/).filter(Boolean);
  let rendered = 0;
  supported.forEach((token) => {
    const label = CONSTANTS.EHR_SUPPORTED_SCOPES[token];
    if (!label) return;
    const card = document.createElement("label");
    card.className = "pf-card";
    const checkbox = document.createElement("input");
    checkbox.type = "checkbox";
    checkbox.name = "scopes";
    checkbox.value = token;
    checkbox.checked = true;
    const body = document.createElement("div");
    body.className = "pf-card__body";
    const title = document.createElement("div");
    title.className = "pf-card__title";
    title.textContent = label;
    body.appendChild(title);
    card.appendChild(checkbox);
    card.appendChild(body);
    container.appendChild(card);
    rendered += 1;
  });
  return rendered;
}

// Where the page goes to sign in; one place, so tests can watch it (jsdom cannot navigate).
const upaBrowser = { go: (url) => window.location.assign(url) };

// Only an https sign-in URL is followed (http too where the server allows it, in development).
function upaSafeSignInUrl(url) {
  try {
    const protocol = new URL(url).protocol;
    return protocol === "https:" || (protocol === "http:" && PATIENT_FACING_CONFIG.allowHttpSignIn === true);
  } catch (e) {
    return false;
  }
}

// The launch: the form's fields as JSON with the bearer token. The server records the attempt,
// sets this browser's callback cookie on the response and answers with the EHR's authorize URL,
// which the page then goes to. A refusal is a reason code, shown on the error route.
async function upaLaunch(form) {
  const fields = new FormData(form);
  pfShowLoading();
  let reason = "sign_in_unavailable"; // also a 5xx, an answer without a reason, or an unsafe URL
  try {
    const response = await fetch(`${API_ENDPOINT}uscdi-patient-access/launch`, {
      method: "POST",
      headers: {
        Authorization: `Bearer ${getStoredToken()}`,
        "Content-Type": "application/json",
        "Cache-Control": "no-cache",
      },
      body: JSON.stringify({
        location_id: fields.get("location_id"),
        data_source_id: fields.get("data_source_id"),
        scopes: fields.getAll("scopes"),
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (response.ok && upaSafeSignInUrl(body.authorizeUrl)) {
      upaBrowser.go(body.authorizeUrl); // assign, not replace: Back returns to the connect step
      return;
    }
    if (response.status === 401) reason = "session_ended";
    else if (!response.ok && body.reason) reason = body.reason;
  } catch (e) {
    // the request never got an answer: sign_in_unavailable stands
  }
  pfHideLoading();
  return pfNav("error", { reason: reason });
}

// Search results as clickable rows (name + address); returns how many were rendered.
function upaRenderBrandResults(container, results, onSelect) {
  container.innerHTML = "";
  if (!results || results.length === 0) {
    const empty = document.createElement("div");
    empty.className = "text-muted p-2";
    empty.textContent = "No hospitals found. Try a different name, city, or state.";
    container.appendChild(empty);
    return 0;
  }
  const list = document.createElement("div");
  list.className = "list-group text-start";
  results.forEach((row) => {
    const item = document.createElement("button");
    item.type = "button";
    item.className = "list-group-item list-group-item-action";
    item.setAttribute("data-brand-result", "");
    const title = document.createElement("div");
    title.className = "fw-bold";
    const facility = row.facilityName && row.facilityName !== row.brandName ? ` — ${row.facilityName}` : "";
    title.textContent = row.brandName + facility;
    const addr = document.createElement("div");
    addr.className = "small text-muted";
    addr.textContent = row.addressText || "";
    item.appendChild(title);
    item.appendChild(addr);
    item.addEventListener("click", () => onSelect(row));
    list.appendChild(item);
  });
  container.appendChild(list);
  return results.length;
}

// Where this hospital's patients pick record types: "host" (our checklist) or "ehr" (the
// vendor's own authorization screen asks per type, so we do not ask twice). The server tells us
// per vendor row (PATIENT_FACING_CONFIG.typeChoiceByVendor) and enforces the same on launch.
function upaTypeChoice(row) {
  const byVendor = (window.PATIENT_FACING_CONFIG || {}).typeChoiceByVendor || {};
  return byVendor[row.vendorName] === "ehr" ? "ehr" : "host";
}

// After the patient picks a hospital: record it in the launch form, then the checklist, or
// straight to the launch when the vendor's own screen asks per type (the server then requests
// every type whatever the form carries).
function upaAfterHospital(row) {
  document.getElementById("launch-location").value = String(row.id);
  document.getElementById("hospital-picker").hidden = true;
  if (upaTypeChoice(row) === "ehr") {
    const going = document.getElementById("launch-status");
    if (going) going.textContent = `Taking you to ${row.brandName || "your healthcare organization"} to sign in…`;
    upaLaunch(document.getElementById("launch-form"));
    return;
  }
  document.getElementById("scope-picker").hidden = false;
  const list = document.getElementById("scope-picker-list");
  const button = document.getElementById("scope-picker-continue");
  upaRenderScopePicker(list, row);
  const update = () => {
    button.disabled = !list.querySelector("input[name=scopes]:checked");
  };
  list.addEventListener("change", update);
  update();
}

// Connect step: the hospital picker, then the scope checklist; Continue launches.
pfClient.connect = async (source) => {
  pfRender("t-connect", { rail: pfRail(1), dataSourceId: String(source.id) });
  const form = document.getElementById("launch-form");
  form.addEventListener("submit", (e) => {
    e.preventDefault();
    upaLaunch(form);
  });
  const picker = {
    container: document.getElementById("hospital-picker"),
    input: document.getElementById("hospital-search"),
    results: document.getElementById("hospital-results"),
  };
  const onSelect = (row) => upaAfterHospital(row);
  const runSearch = async () => {
    upaRenderBrandResults(picker.results, await upaSearchBrands(picker.input.value), onSelect);
  };
  let timer = null;
  picker.input.addEventListener("input", () => {
    if (timer) clearTimeout(timer);
    timer = setTimeout(runSearch, 200);
  });
  picker.container.hidden = false;
  await runSearch();
};

// One line per query-table row: what the pull has done for it so far.
function upaTypeLine(t) {
  if (t.status === "ok") return `${t.label}: ${t.count} record(s)`;
  if (t.status === "error") return `${t.label}: could not fetch`;
  if (t.status === "skipped") return `${t.label}: skipped`;
  if (t.status === "not_requested") return `${t.label}: not selected`;
  if (t.status === "not_granted") return `${t.label}: not shared by your organization`;
  return `${t.label}: waiting…`;
}

function upaRecords(job) {
  return job.types.reduce((sum, t) => sum + (t.count || 0), 0);
}

// The progress card's one line: where the pull is, in the patient's terms.
function upaProgressCopy(job) {
  const settled = job.types.filter((t) => t.status !== "pending");
  if (job.status === "queued") return "Starting…";
  if (job.status === "fetching") {
    return `${settled.length} of ${job.types.length} record types fetched · ${upaRecords(job)} records so far`;
  }
  if (job.status === "failed") return "Delivery hit a problem; retrying automatically.";
  return `Delivering ${upaRecords(job)} records to your study…`;
}

function upaJobText(job) {
  const lines = [`Connected to ${job.source.brand}`, ""].concat(job.types.map(upaTypeLine));
  if (job.documents && job.documents.stored) lines.push("", `Documents: ${job.documents.stored}`);
  return lines.join("\n");
}

// What a failed pull asks of the patient, by the job's failure kind (the server's own text is for
// the operator, never shown).
const UPA_FAILURE_COPY = {
  retry_later: ["Sending is taking longer", "Your records will be sent automatically. You can close this page."],
  reconnect: [
    "Please connect again",
    "Your healthcare organization's sign-in ended before all your records were downloaded, so nothing was sent.",
  ],
  contact_study: ["We couldn't send your records", "Please contact your study team."],
  consent_withdrawn: ["Nothing was sent", "You are no longer sharing your records with this study."],
};

function upaFailureCopy(job) {
  return UPA_FAILURE_COPY[job.failureKind] || ["We couldn't send your records", "The transfer did not finish."];
}

// Status route (?route=status&job=...&source=...): the importing screen, fed by polling the job,
// then the done screen or the error callout. The pull runs on the server, so the patient may
// close the page; it keeps going.
async function upaStatus(params) {
  await renderImporting();
  const lede = document.querySelector("#pf_main .pf-lede");
  if (lede) lede.textContent = "Securely sending your records to your study. You can close this page; it keeps going.";
  const out = document.getElementById("out");
  const copy = document.querySelector("#pf_main .pf-import-card__copy");
  const retryHref = pfUrl("connect", { source: String(params.source || PATIENT_FACING_CONFIG.dataSourceIds[0]) });
  for (;;) {
    const job = await pfApi("GET", `uscdi-patient-access/jobs/${encodeURIComponent(params.job || "")}`);
    if (out) out.textContent = upaJobText(job);
    if (copy) copy.textContent = upaProgressCopy(job);
    pfHideLoading(); // the router keeps its overlay up until a route returns; this one keeps polling
    if (job.status === "done") {
      return pfNav("done", { source: String(params.source || job.dataSourceId), job: job.id }, true);
    }
    if (job.terminal) {
      const [title, message] = upaFailureCopy(job);
      return showFlowError(title, message, {
        retryLabel: job.failureKind === "reconnect" ? "Connect again" : "Choose a different organization",
        retryHref: retryHref,
      });
    }
    if (pfRouteAndParams().route !== "status") return; // the patient navigated away
    await new Promise((resolve) => setTimeout(resolve, UPA_POLL_MS));
  }
}

// Done route: the receipt comes from the job (this client writes nothing to JHE's FHIR store, so
// the shell's FhirSource counts would read zero). Without a job, the shell's own done screen.
const upaShellDone = PF_ROUTES.done;

// A type that was fetched shows its count, zero included; "Not synced" is only for types that
// were not requested, not granted or failed.
function upaReceipt(job) {
  const counts = {};
  job.types
    .filter((t) => t.status === "ok")
    .forEach((t) => {
      counts[t.resourceType] = (counts[t.resourceType] || 0) + (t.count || 0);
    });
  return pfReceipt(counts, PATIENT_FACING_CONFIG.expectedResourceTypes);
}

async function upaDone(params) {
  if (!params.job) return upaShellDone(params);
  const job = await pfApi("GET", `uscdi-patient-access/jobs/${encodeURIComponent(params.job)}`);
  const sourceName = (PATIENT_FACING_CONFIG.sourceLabels || {})[String(job.dataSourceId)] || "Clinical records";
  pfRender("t-done", {
    lede: `Your records from ${job.source.brand} were delivered to your study. You can manage or stop sharing anytime.`,
    rows: [{ name: sourceName, detail: `${job.source.location || job.source.brand} · ${upaRecords(job)} records` }],
    receipt: upaReceipt(job),
  });
}

// The patient's connected hospitals for a data source: each FhirSource with its latest delivered
// pull. A patient may connect any number; each pull is its own bundle under its own hospital.
function upaHospitals(fhirSources, jobs, dataSourceId) {
  const latestBy = {};
  jobs
    .filter((j) => j.status === "done")
    .forEach((j) => {
      if (!latestBy[j.fhirSourceId]) latestBy[j.fhirSourceId] = j; // newest first
    });
  return fhirSources
    .filter((fs) => String(fs.dataSource) === String(dataSourceId))
    .sort((a, b) => a.id - b.id)
    .map((fs) => {
      const latest = latestBy[fs.id] || null;
      const when = latest && latest.finishedAt ? new Date(latest.finishedAt).toLocaleDateString() : null;
      return {
        name: fs.ehrBrandLocationName || fs.label,
        detail: latest ? `${upaRecords(latest)} records sent${when ? ` on ${when}` : ""}` : "Not yet synced",
        receipt: latest ? upaReceipt(latest) : null,
      };
    });
}

// Manage route: every connected hospital with what its latest pull delivered, from this client's
// own jobs, and the way to connect another.
async function upaManage(params) {
  const source = await pfSource(params.source);
  if (!source || !source.consented.length) return pfNav("hub", {}, true);
  const fhirSources = await pfFhirSources();
  const jobs = (await pfApi("GET", `uscdi-patient-access/jobs?source=${encodeURIComponent(source.id)}`)).results || [];
  pfRender("t-upa-manage", {
    sourceId: source.id,
    sourceName: source.name,
    icon: source.icon,
    hospitals: upaHospitals(fhirSources, jobs, source.id),
  });
}

// The hub card: this client keeps no records in JHE, so the shell's "N records" (counted from
// JHE's FHIR store) would always read 0. One hospital is named; several are counted, so the card
// never names one of them for all.
function upaCardDesc(source, fhirSources) {
  const mine = (fhirSources || []).filter((fs) => String(fs.dataSource) === String(source.id));
  const labels = source.labels.join(", ");
  if (!mine.length) return labels;
  if (mine.length === 1) return `${mine[0].ehrBrandLocationName || mine[0].label} · ${labels}`;
  return `${mine.length} healthcare organizations · ${labels}`;
}

// The hub, as the shell renders it, with this client's card description over every FhirSource
// of a data source rather than the newest one.
async function upaHub() {
  const sources = await pfSourcesNow();
  const fhirSources = sources.some((s) => s.isConsented) ? await pfFhirSources() : [];
  const studies = pfUniqueSorted([].concat.apply([], sources.map((s) => s.studies)));
  pfRender("t-hub", {
    eyebrow: studies.length === 1 ? studies[0] : "Your studies",
    cards: sources.map((source) => ({
      id: source.id,
      title: source.name,
      desc: upaCardDesc(source, source.isConsented ? fhirSources : []),
      icon: source.icon,
      on: source.isConsented,
      badge: source.isConsented ? "Consented" : "Not consented",
      route: source.isConsented ? "manage" : "consent",
    })),
  });
}

// Every reason the server sends (Reason in core/views/uscdi_patient_access.py), and the client's
// own, in the patient's words. `retry: false` where trying again cannot help: only Back is offered.
const UPA_REASON_COPY = {
  unknown_selection: {
    title: "We couldn't find that organization",
    message: "Please search for your healthcare organization again.",
    retry: true,
  },
  not_configured: {
    title: "Not available yet",
    message: "Sharing records from this healthcare organization is not available yet.",
    retry: true,
  },
  sign_in_unavailable: {
    title: "We couldn't reach your healthcare organization",
    message: "Its sign-in is not responding right now. Please try again later.",
    retry: true,
  },
  attempt_invalid: {
    title: "This sign-in has expired",
    message: "It took too long or was already used. Please connect again.",
    retry: true,
  },
  declined: {
    title: "Nothing was shared",
    message: "Sharing was not approved at your healthcare organization. You can connect again whenever you're ready.",
    retry: true,
  },
  no_code: {
    title: "We couldn't connect to your healthcare organization",
    message: "The sign-in did not finish. Please try again.",
    retry: true,
  },
  not_available: {
    title: "Not available",
    message: "Sharing records from your healthcare organization is not available here yet.",
    retry: false,
  },
  not_a_patient: {
    title: "We couldn't start the connection",
    message: "Open the link from your invitation email to continue.",
    retry: false,
  },
  invalid_request: {
    title: "We couldn't start the connection",
    message: "Please choose your healthcare organization again.",
    retry: true,
  },
  source_unavailable: {
    title: "We couldn't start the connection",
    message: "This study connection is not available.",
    retry: false,
  },
  consent_required: {
    title: "We couldn't start the connection",
    message: "Please agree to share your records with your study first.",
    retry: true,
  },
  no_types: {
    title: "We couldn't start the connection",
    message: "Please choose at least one kind of record to share.",
    retry: true,
  },
  browser_mismatch: {
    title: "Please connect from the same browser",
    message: "Open the link from your invitation email again in this browser, and connect from there.",
    retry: false,
  },
  consent_withdrawn: {
    title: "Nothing was fetched",
    message: "You are no longer sharing records with this study.",
    retry: false,
  },
  identifier_conflict: {
    title: "We couldn't connect your records",
    message: "This health record is already linked to another person in this study. Please contact your study team.",
    retry: false,
  },
  // The client's own.
  session_ended: {
    title: "Your session has ended",
    message: "Open the link from your invitation email to continue.",
    retry: false,
  },
  signed_out: {
    title: "You're signed out",
    message: "You can close this page. To share again, open the link from your invitation email.",
    retry: false,
  },
};
const UPA_UNKNOWN_REASON = { title: "Something went wrong", message: "Please try again.", retry: true };

function upaReasonCopy(reason) {
  return Object.prototype.hasOwnProperty.call(UPA_REASON_COPY, reason) ? UPA_REASON_COPY[reason] : UPA_UNKNOWN_REASON;
}

// Error route (?route=error&reason=...): the reason's copy. Only the reason is read; a title or
// message in the URL is never shown, so no one can put their own words on this screen. That
// includes the tab: the shell titles it from a URL `title`, so it is retitled here.
function upaError(params) {
  const copy = upaReasonCopy(params.reason);
  document.title = `${PATIENT_FACING_CONFIG.siteTitle} - ${copy.title}`;
  return showFlowError(copy.title, copy.message, {
    retryHref: pfUrl("connect", { source: String(PATIENT_FACING_CONFIG.dataSourceIds[0]) }),
    noRetry: !copy.retry,
  });
}

// The status route in a tab without the token: the EHR returned the browser to a new tab, say.
// The pull runs anyway; this tab just cannot follow it.
async function upaSendingElsewhere() {
  await renderImporting();
  const lede = document.querySelector("#pf_main .pf-lede");
  if (lede) lede.textContent = "Your records are being sent to your study. You can close this page; it keeps going.";
  const copy = document.querySelector("#pf_main .pf-import-card__copy");
  if (copy) copy.textContent = "To follow progress, open the link from your invitation email.";
}

// Page entry point. Without a token (and no invitation code to redeem), two screens still show:
// an error the server redirected here and a pull being sent. Everything else is the shell's,
// whose gate says the session has ended.
function upaStart() {
  pfRegisterPartials();
  const current = pfRouteAndParams();
  if (PATIENT_FACING_CONFIG.available === false) return upaError({ reason: "not_available" });
  if (!current.params.code && !getStoredToken()) {
    if (current.route === "error") return upaError(current.params);
    if (current.route === "status") return upaSendingElsewhere();
  }
  return patientApp();
}

// Back from the EHR's sign-in can restore this page from the browser's back-forward cache as it
// was left: the loading overlay up and, for a vendor whose own screen asks per type, the picker
// already hidden. Take the overlay down and start the connect step afresh.
window.addEventListener("pageshow", (e) => {
  if (!e.persisted) return;
  pfHideLoading();
  if (pfRouteAndParams().route === "connect") pfRetry();
});

// Sign out: the server revokes the token (and the refresh token issued with it), then the tab
// forgets it. A revoke that fails (the token already dead, the network down) still signs out here.
async function upaSignOut() {
  pfShowLoading();
  try {
    await pfApi("POST", "uscdi-patient-access/sign-out");
  } catch (e) {
    // signing out goes on
  }
  clearStoredToken();
  return pfNav("error", { reason: "signed_out" }, true);
}

// A Sign out button under the hub, done and manage screens.
function upaAppendSignOut() {
  const main = document.getElementById("pf_main");
  if (!main || main.querySelector(".upa-sign-out")) return;
  const actions = document.createElement("div");
  actions.className = "upa-sign-out pf-actions";
  const button = document.createElement("button");
  button.type = "button";
  button.className = "pf-btn pf-btn--ghost";
  button.textContent = "Sign out";
  button.addEventListener("click", () => upaSignOut());
  actions.appendChild(button);
  main.appendChild(actions);
}

function upaWithSignOut(route) {
  return async (params) => {
    const out = await route(params);
    upaAppendSignOut();
    return out;
  };
}

window.PF_ROUTES.status = upaStatus;
window.PF_ROUTES.done = upaWithSignOut(upaDone);
window.PF_ROUTES.error = upaError;
window.PF_ROUTES.manage = upaWithSignOut(upaManage);
window.PF_ROUTES.hub = upaWithSignOut(upaHub);
window.PF_ROUTE_TITLES.status = "Sending records";

// Exposed for unit tests; browser runs load this as a plain <script> and ignore it.
if (typeof window !== "undefined") {
  window.upaSearchBrands = upaSearchBrands;
  window.upaBrowser = upaBrowser;
  window.upaSafeSignInUrl = upaSafeSignInUrl;
  window.upaLaunch = upaLaunch;
  window.upaRenderBrandResults = upaRenderBrandResults;
  window.upaRenderScopePicker = upaRenderScopePicker;
  window.upaTypeChoice = upaTypeChoice;
  window.upaAfterHospital = upaAfterHospital;
  window.upaTypeLine = upaTypeLine;
  window.upaProgressCopy = upaProgressCopy;
  window.upaJobText = upaJobText;
  window.upaStatus = upaStatus;
  window.upaReceipt = upaReceipt;
  window.upaDone = upaDone;
  window.upaError = upaError;
  window.upaReasonCopy = upaReasonCopy;
  window.UPA_REASON_COPY = UPA_REASON_COPY;
  window.upaSendingElsewhere = upaSendingElsewhere;
  window.upaManage = upaManage;
  window.upaCardDesc = upaCardDesc;
  window.upaHospitals = upaHospitals;
  window.upaHub = upaHub;
  window.upaStart = upaStart;
  window.upaFailureCopy = upaFailureCopy;
  window.upaAppendSignOut = upaAppendSignOut;
  window.upaSignOut = upaSignOut;
}
