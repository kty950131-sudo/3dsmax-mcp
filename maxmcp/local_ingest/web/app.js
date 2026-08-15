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
const pollIntervalMs = 1000;

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
  stopPolling();
  setBusy(true);
  setStatus("로컬 정리가 필요합니다.", 0);
  cancelAction.textContent = "로컬 정리 다시 시도";
  cancelAction.disabled = cleanupInFlight;
}

function renderCancelled() {
  terminal = true;
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

function scheduleSessionPoll(expectedGeneration = generation) {
  stopPolling();
  if (!terminal || expectedGeneration !== generation) return;
  pollTimer = setTimeout(async () => {
    pollTimer = null;
    if (!terminal || expectedGeneration !== generation) return;
    try {
      const response = await fetch("/api/session", { cache: "no-store" });
      if (response.status !== 200) throw new Error("session_unavailable");
      const payload = parseSession(await response.json());
      if (expectedGeneration !== generation) return;
      renderSession(payload);
    } catch (_error) {
      if (terminal && expectedGeneration === generation) {
        scheduleSessionPoll(expectedGeneration);
      }
    }
  }, pollIntervalMs);
}

function renderCancelling() {
  terminal = true;
  setBusy(true);
  setStatus("취소 중입니다.", 0);
  cancelAction.textContent = "취소 중";
  cancelAction.disabled = true;
  scheduleSessionPoll();
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
    scheduleSessionPoll();
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
  const response = await fetch("/api/session", { cache: "no-store" });
  if (response.status !== 200) throw new Error("session_unavailable");
  renderSession(parseSession(await response.json()));
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

async function requestCleanup() {
  if (cleanupInFlight) return;
  terminal = true;
  generation += 1;
  const uploadRequest = activeUpload;
  activeUpload = null;
  if (uploadRequest) {
    try { uploadRequest.abort(); } catch (_error) { /* terminal state still wins */ }
  }
  setBusy(true);
  cleanupInFlight = true;
  cancelAction.disabled = true;
  try {
    if (!csrfToken) throw new Error("session_unavailable");
    const response = await fetch("/api/cancel", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken },
      body: "",
    });
    const result = parseCleanup(response, await response.json());
    if (result.status === "cancelled" && result.cleaned) {
      renderCancelled();
      return;
    }
    if (result.status === "cancelling") renderCancelling();
    else renderCleanupRequired();
  } catch (_error) {
    renderCancelling();
  } finally {
    cleanupInFlight = false;
    if (cancelAction.textContent === "로컬 정리 다시 시도") {
      cancelAction.disabled = false;
    }
  }
}

sourceInput.addEventListener("change", () => {
  const file = sourceInput.files && sourceInput.files[0];
  if (!file || terminal) return;
  upload(file);
});

cancelAction.addEventListener("click", requestCleanup);

loadSession().catch(() => {
  if (terminal) return;
  setBusy(true);
  setStatus("로컬 앱 연결을 확인해 주세요.", 0);
});
