// ────────────────────────────────────────────────────
// USCDI Patient Access client: the patient's records pulled from their hospital's patient portal
// on the server and delivered to the study's storage. Registers, for the shared patient-facing
// app (core/static/common/js/patient-facing.js):
//   - the connect step: hospital picker, then "choose what to share" (copied from the EHR Patient
//     Portal client, not shared with it), then a real form post to the server's launch, which
//     redirects to the EHR's sign-in;
//   - the status route the server's callback redirects to, polling the job;
//   - its own done and error screens (the done screen reads the job, not JHE's FHIR store, which
//     this client never writes to).
// The page runs the shell in session mode (PATIENT_FACING_CONFIG.auth === "session"): every
// request is same-origin with the patient's session cookie; pfApi adds the CSRF token.
// ────────────────────────────────────────────────────

const UPA_POLL_MS = 2000;

// Search hospital facilities for the picker. Returns an array of rows (or [] on any failure).
async function upaSearchBrands(query) {
  try {
    const out = await pfApi("GET", `uscdi-patient-access/brands?q=${encodeURIComponent(query || "")}`);
    return (out && out.results) || [];
  } catch (e) {
    return [];
  }
}

// One pre-checked row per scope the vendor supports (row.supportedScopes), labelled from
// CONSTANTS.EHR_SUPPORTED_SCOPES. Each checkbox is a form field (name "scopes"), so the ticked
// ones are what the launch form posts. Returns how many rows were rendered.
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

// Submit the launch form the way a click on its button would (firing "submit", so the loading
// overlay shows); browsers without requestSubmit get a plain submit.
function upaSubmitLaunch(form) {
  if (typeof form.requestSubmit === "function") {
    form.requestSubmit();
  } else {
    pfShowLoading();
    form.submit();
  }
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
// straight to the sign-in when the vendor's own screen asks per type (the server then requests
// every type whatever the form carries).
function upaAfterHospital(row) {
  document.getElementById("launch-location").value = String(row.id);
  document.getElementById("hospital-picker").hidden = true;
  if (upaTypeChoice(row) === "ehr") {
    const going = document.getElementById("launch-status");
    if (going) going.textContent = `Taking you to ${row.brandName || "your healthcare organization"} to sign in…`;
    upaSubmitLaunch(document.getElementById("launch-form"));
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

// Connect step: the hospital picker, then the scope checklist; Continue submits the launch form.
pfClient.connect = async (source) => {
  pfRender("t-connect", {
    rail: pfRail(1),
    launchUrl: PATIENT_FACING_CONFIG.launchUrl,
    csrfToken: PATIENT_FACING_CONFIG.csrfToken,
    dataSourceId: String(source.id),
  });
  document.getElementById("launch-form").addEventListener("submit", () => pfShowLoading());
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

function upaReceipt(job) {
  const counts = {};
  job.types
    .filter((t) => t.status === "ok" && t.count)
    .forEach((t) => {
      counts[t.resourceType] = (counts[t.resourceType] || 0) + t.count;
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

// Manage route: what the latest pull delivered, from this client's own jobs.
async function upaManage(params) {
  const source = await pfSource(params.source);
  if (!source || !source.consented.length) return pfNav("hub", {}, true);
  const jobs = (await pfApi("GET", `uscdi-patient-access/jobs?source=${encodeURIComponent(source.id)}`)).results || [];
  const latest = jobs.filter((j) => j.status === "done")[0] || null;
  pfRender("t-manage", {
    sourceId: source.id,
    sourceName: source.name,
    icon: source.icon,
    detail: latest ? `${latest.source.location || latest.source.brand} · ${upaRecords(latest)} records sent` : null,
    rows: source.consentedLabels,
    receipt: latest ? upaReceipt(latest) : null,
  });
}

// The hub card: this client keeps no records in JHE, so the shell's "N records" (counted from
// JHE's FHIR store) would always read 0; the facility and labels alone.
const upaShellCardDesc = pfCardDesc;
function upaCardDesc(source, fhirSource) {
  if (!fhirSource || !fhirSource.ehrBrandLocationName) return upaShellCardDesc(source, fhirSource);
  return `${fhirSource.ehrBrandLocationName} · ${source.labels.join(", ")}`;
}

// Error route: errors the server carried in the session (never in the URL) come in the page
// config; the shell's own ?title=&message= form still works.
function upaError(params) {
  const flow = PATIENT_FACING_CONFIG.flowError;
  if (flow) {
    return showFlowError(flow.title, flow.message, {
      retryHref: pfUrl("connect", { source: String(PATIENT_FACING_CONFIG.dataSourceIds[0]) }),
      noRetry: flow.retry === false,
    });
  }
  return renderError(params);
}

// Page entry point: a carried error (an invalid link, a session that ended, a failed callback)
// is shown instead of the app; otherwise the shell runs as usual.
function upaStart() {
  if (PATIENT_FACING_CONFIG.flowError) {
    pfRegisterPartials();
    return upaError({});
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

// Sign out: a small form under the hub, done and manage screens. Ends the session on the server;
// the 12-hour expiry remains the backstop for a patient who just closes the page.
function upaAppendSignOut() {
  const main = document.getElementById("pf_main");
  const url = PATIENT_FACING_CONFIG.signOutUrl;
  if (!main || !url || main.querySelector("form.upa-sign-out")) return;
  const form = document.createElement("form");
  form.className = "upa-sign-out pf-actions";
  form.method = "post";
  form.action = url;
  const csrf = document.createElement("input");
  csrf.type = "hidden";
  csrf.name = "csrfmiddlewaretoken";
  csrf.value = PATIENT_FACING_CONFIG.csrfToken || "";
  const button = document.createElement("button");
  button.type = "submit";
  button.className = "pf-btn pf-btn--ghost";
  button.textContent = "Sign out";
  form.appendChild(csrf);
  form.appendChild(button);
  main.appendChild(form);
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
window.PF_ROUTES.hub = upaWithSignOut(window.PF_ROUTES.hub);
window.pfCardDesc = upaCardDesc;
window.PF_ROUTE_TITLES.status = "Sending records";

// Exposed for unit tests; browser runs load this as a plain <script> and ignore it.
if (typeof window !== "undefined") {
  window.upaSearchBrands = upaSearchBrands;
  window.upaSubmitLaunch = upaSubmitLaunch;
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
  window.upaManage = upaManage;
  window.upaCardDesc = upaCardDesc;
  window.upaStart = upaStart;
  window.upaFailureCopy = upaFailureCopy;
  window.upaAppendSignOut = upaAppendSignOut;
}
