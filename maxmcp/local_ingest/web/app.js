const sourceInput = document.querySelector("#source");
const selectAction = document.querySelector("#select-action");
const cancelAction = document.querySelector("#cancel-action");
const statusText = document.querySelector("#status");
const progressBar = document.querySelector("#progress-bar");

const sessionStates = new Set([
  "ready", "receiving", "accepted", "cancelling",
  "cancelled", "cleanup_required", "closed",
]);

let csrfToken = "";
let activeUpload = null;
let generation = 0;
let terminal = false;
let cleanupInFlight = false;
let pollTimer = null;
let cancelIntent = false;
let cancelAttempts = 0;
let nonterminalPolls = 0;
let cancelAcknowledged = false;
let disposed = false;
const activeFetchControllers = new Set();
const pollIntervalMs = 1000;
const requestTimeoutMs = 5000;
const maxCancelAttempts = 2;
const pollsBeforeCancelRetry = 2;

function setStatus(message, progress) {
  statusText.textContent = message;
  progressBar.style.width = `${Math.max(0, Math.min(progress, 100))}%`;
}

function setBusy(value) {
  sourceInput.disabled = value;
  selectAction.setAttribute("aria-disabled", String(value));
}

function exactKeys(value, expected) {
  if (!value || typeof value !== "object" || Array.isArray(value)) return false;
  const keys = Object.keys(value).sort();
  return keys.length === expected.length && keys.every((key, index) => key === expected[index]);
}

function parseSession(payload) {
  const keys = ["cleaned", "csrfToken", "sizeBytes", "state"];
  if (
    !exactKeys(payload, keys)
    || typeof payload.csrfToken !== "string"
    || payload.csrfToken.length < 16
    || !sessionStates.has(payload.state)
    || (payload.sizeBytes !== null && (!Number.isSafeInteger(payload.sizeBytes) || payload.sizeBytes <= 0))
    || typeof payload.cleaned !== "boolean"
  ) throw new Error("invalid_session");
  return payload;
}

function parseCleanup(response, payload) {
  if (!exactKeys(payload, ["cleaned", "status"]) || typeof payload.cleaned !== "boolean") {
    throw new Error("invalid_cleanup");
  }
  if (response.status === 200 && payload.status === "cancelled" && payload.cleaned === true) {
    return payload;
  }
  if (
    response.status === 202
    && (payload.status === "cancelling" || payload.status === "cleanup_required")
    && payload.cleaned === false
  ) return payload;
  throw new Error("invalid_cleanup");
}

function encodeDisplayName(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

function renderCleanupRequired() {
  terminal = true;
  cancelIntent = false;
  cancelAcknowledged = false;
  stopPolling();
  setBusy(true);
  setStatus("로컬 정리가 필요합니다.", 0);
  cancelAction.textContent = "로컬 정리 다시 시도";
  cancelAction.disabled = cleanupInFlight;
}

function renderCancelled() {
  terminal = true;
  cancelIntent = false;
  cancelAcknowledged = false;
  stopPolling();
  setBusy(true);
  setStatus("로컬 정리가 끝났습니다. 이 창을 닫아도 됩니다.", 0);
  cancelAction.textContent = "정리 완료";
  cancelAction.disabled = true;
}

function uploadFailed(message, requestGeneration) {
  if (terminal || generation !== requestGeneration) return;
  activeUpload = null;
  setBusy(false);
  sourceInput.value = "";
  setStatus(message, 0);
}

function stopPolling() {
  if (pollTimer !== null) {
    clearTimeout(pollTimer);
    pollTimer = null;
  }
}

async function fetchJson(path, options = {}) {
  const controller = new AbortController();
  activeFetchControllers.add(controller);
  let timeoutId;
  try {
    const operation = (async () => {
      const response = await fetch(path, { ...options, signal: controller.signal });
      return { response, payload: await response.json() };
    })();
    const timeout = new Promise((_, reject) => {
      timeoutId = setTimeout(() => {
        controller.abort();
        reject(new Error("request_timeout"));
      }, requestTimeoutMs);
    });
    return await Promise.race([operation, timeout]);
  } finally {
    if (timeoutId !== undefined) clearTimeout(timeoutId);
    activeFetchControllers.delete(controller);
  }
}

function scheduleSessionPoll(expectedGeneration = generation) {
  stopPolling();
  if (disposed || !terminal || expectedGeneration !== generation) return;
  pollTimer = setTimeout(async () => {
    pollTimer = null;
    if (disposed || !terminal || expectedGeneration !== generation) return;
    try {
      const { response, payload } = await fetchJson("/api/session", { cache: "no-store" });
      if (response.status !== 200) throw new Error("session_unavailable");
      const session = parseSession(payload);
      if (disposed || expectedGeneration !== generation) return;
      renderSession(session);
    } catch (_error) {
      observeUnconfirmedCancel(expectedGeneration);
    }
  }, pollIntervalMs);
}

function renderCancelling(acknowledged = true) {
  terminal = true;
  cancelIntent = true;
  if (acknowledged) {
    cancelAcknowledged = true;
    nonterminalPolls = 0;
  }
  setBusy(true);
  setStatus("취소 중입니다.", 0);
  cancelAction.textContent = "취소 중";
  cancelAction.disabled = true;
  scheduleSessionPoll();
}

function renderCancelDeliveryFailed() {
  terminal = true;
  stopPolling();
  setBusy(true);
  setStatus("취소 요청을 보내지 못했습니다.", 0);
  cancelAction.textContent = "취소 다시 시도";
  cancelAction.disabled = false;
}

function observeUnconfirmedCancel(expectedGeneration = generation) {
  if (!terminal || !cancelIntent || expectedGeneration !== generation) return;
  if (cancelAcknowledged) {
    scheduleSessionPoll(expectedGeneration);
    return;
  }
  nonterminalPolls += 1;
  if (nonterminalPolls < pollsBeforeCancelRetry) {
    scheduleSessionPoll(expectedGeneration);
    return;
  }
  nonterminalPolls = 0;
  if (cancelAttempts < maxCancelAttempts) {
    void sendCancelRequest(expectedGeneration);
    return;
  }
  renderCancelDeliveryFailed();
}

function renderSession(payload) {
  csrfToken = payload.csrfToken;
  if (payload.state === "cancelled" || payload.state === "closed") {
    if (payload.cleaned) renderCancelled();
    else renderCleanupRequired();
    return;
  }
  if (payload.state === "cleanup_required") {
    renderCleanupRequired();
    return;
  }
  if (payload.state === "cancelling") {
    renderCancelling();
    return;
  }
  if (terminal) {
    observeUnconfirmedCancel();
    return;
  }
  if (payload.state === "accepted") {
    setBusy(true);
    setStatus("영상이 준비됐습니다.", 100);
    return;
  }
  if (payload.state === "receiving") {
    setBusy(true);
    setStatus("영상을 이 PC로 가져오고 있습니다.", 0);
    return;
  }
  setBusy(false);
  setStatus("영상을 선택해 주세요.", 0);
}

async function loadSession() {
  const { response, payload } = await fetchJson("/api/session", { cache: "no-store" });
  if (response.status !== 200) throw new Error("session_unavailable");
  if (!disposed) renderSession(parseSession(payload));
}

function upload(file) {
  if (terminal || activeUpload) return;
  const requestGeneration = generation;
  setBusy(true);
  setStatus("영상을 이 PC로 가져오고 있습니다.", 0);
  let request;
  try {
    request = new XMLHttpRequest();
    activeUpload = request;
    request.open("POST", "/api/source");
    request.setRequestHeader("X-CSRF-Token", csrfToken);
    request.setRequestHeader("X-Artoke-Filename", encodeDisplayName(file.name));
    request.setRequestHeader("Content-Type", "application/octet-stream");
  } catch (_error) {
    uploadFailed("파일 이름을 확인한 뒤 다시 시도해 주세요.", requestGeneration);
    return;
  }
  const current = () => (
    !terminal && generation === requestGeneration && activeUpload === request
  );
  request.upload.addEventListener("progress", (event) => {
    if (current() && event.lengthComputable) {
      setStatus("영상을 이 PC로 가져오고 있습니다.", event.loaded / event.total * 100);
    }
  });
  request.addEventListener("load", () => {
    if (!current()) return;
    activeUpload = null;
    if (request.status === 201) {
      setStatus("영상이 준비됐습니다.", 100);
      return;
    }
    uploadFailed("영상을 가져오지 못했습니다. 다시 시도해 주세요.", requestGeneration);
  });
  request.addEventListener("error", () => {
    if (current()) uploadFailed("연결을 확인한 뒤 다시 시도해 주세요.", requestGeneration);
  });
  try {
    request.send(file);
  } catch (_error) {
    uploadFailed("영상을 가져오지 못했습니다. 다시 시도해 주세요.", requestGeneration);
  }
}

async function sendCancelRequest(expectedGeneration) {
  if (cleanupInFlight) return;
  cleanupInFlight = true;
  cancelAttempts += 1;
  cancelAction.disabled = true;
  try {
    if (!csrfToken) throw new Error("session_unavailable");
    const { response, payload } = await fetchJson("/api/cancel", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken },
      body: "",
    });
    const result = parseCleanup(response, payload);
    if (disposed || expectedGeneration !== generation) return;
    if (result.status === "cancelled" && result.cleaned) {
      renderCancelled();
      return;
    }
    if (result.status === "cancelling") renderCancelling();
    else renderCleanupRequired();
  } catch (_error) {
    if (expectedGeneration !== generation) return;
    if (cancelAttempts >= maxCancelAttempts) renderCancelDeliveryFailed();
    else renderCancelling(false);
  } finally {
    cleanupInFlight = false;
    if (cancelAction.textContent === "로컬 정리 다시 시도") {
      cancelAction.disabled = false;
    }
  }
}

async function requestCleanup() {
  if (disposed || cleanupInFlight) return;
  terminal = true;
  cancelIntent = true;
  cancelAttempts = 0;
  nonterminalPolls = 0;
  cancelAcknowledged = false;
  generation += 1;
  stopPolling();
  const uploadRequest = activeUpload;
  activeUpload = null;
  if (uploadRequest) {
    try { uploadRequest.abort(); } catch (_error) { /* terminal state still wins */ }
  }
  setBusy(true);
  setStatus("취소 중입니다.", 0);
  cancelAction.textContent = "취소 중";
  cancelAction.disabled = true;
  await sendCancelRequest(generation);
}

sourceInput.addEventListener("change", () => {
  const file = sourceInput.files && sourceInput.files[0];
  if (!file || terminal) return;
  upload(file);
});

cancelAction.addEventListener("click", requestCleanup);
globalThis.addEventListener?.("pagehide", () => {
  disposed = true;
  generation += 1;
  const uploadRequest = activeUpload;
  activeUpload = null;
  if (uploadRequest) {
    try { uploadRequest.abort(); } catch (_error) { /* page teardown still wins */ }
  }
  cancelIntent = false;
  stopPolling();
  for (const controller of activeFetchControllers) controller.abort();
  activeFetchControllers.clear();
});

loadSession().catch(() => {
  if (disposed || terminal) return;
  setBusy(true);
  setStatus("로컬 앱 연결을 확인해 주세요.", 0);
});
