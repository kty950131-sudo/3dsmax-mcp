const sourceInput = document.querySelector("#source");
const selectAction = document.querySelector("#select-action");
const cancelAction = document.querySelector("#cancel-action");
const statusText = document.querySelector("#status");
const progressBar = document.querySelector("#progress-bar");

let csrfToken = "";
let busy = false;

function setStatus(message, progress) {
  statusText.textContent = message;
  progressBar.style.width = `${Math.max(0, Math.min(progress, 100))}%`;
}

function setBusy(value) {
  busy = value;
  sourceInput.disabled = value;
  selectAction.setAttribute("aria-disabled", String(value));
}

function encodeDisplayName(value) {
  const bytes = new TextEncoder().encode(value);
  let binary = "";
  for (const byte of bytes) binary += String.fromCharCode(byte);
  return btoa(binary).replaceAll("+", "-").replaceAll("/", "_").replace(/=+$/u, "");
}

function uploadFailed(message) {
  setBusy(false);
  sourceInput.value = "";
  setStatus(message, 0);
}

async function loadSession() {
  const response = await fetch("/api/session", { cache: "no-store" });
  if (!response.ok) throw new Error("session_unavailable");
  const payload = await response.json();
  csrfToken = payload.csrfToken;
  if (payload.state === "accepted") {
    setBusy(true);
    setStatus("영상이 준비됐습니다.", 100);
  }
}

function upload(file) {
  setBusy(true);
  setStatus("영상을 이 PC로 가져오고 있습니다.", 0);
  let request;
  try {
    request = new XMLHttpRequest();
    request.open("POST", "/api/source");
    request.setRequestHeader("X-CSRF-Token", csrfToken);
    request.setRequestHeader("X-Artoke-Filename", encodeDisplayName(file.name));
    request.setRequestHeader("Content-Type", "application/octet-stream");
  } catch (_error) {
    uploadFailed("파일 이름을 확인한 뒤 다시 시도해 주세요.");
    return;
  }
  request.upload.addEventListener("progress", (event) => {
    if (event.lengthComputable) setStatus("영상을 이 PC로 가져오고 있습니다.", event.loaded / event.total * 100);
  });
  request.addEventListener("load", () => {
    if (request.status === 201) {
      setStatus("영상이 준비됐습니다.", 100);
      return;
    }
    uploadFailed("영상을 가져오지 못했습니다. 다시 시도해 주세요.");
  });
  request.addEventListener("error", () => {
    uploadFailed("연결을 확인한 뒤 다시 시도해 주세요.");
  });
  try {
    request.send(file);
  } catch (_error) {
    uploadFailed("영상을 가져오지 못했습니다. 다시 시도해 주세요.");
  }
}

sourceInput.addEventListener("change", () => {
  const file = sourceInput.files && sourceInput.files[0];
  if (!file || busy) return;
  upload(file);
});

cancelAction.addEventListener("click", async () => {
  if (!csrfToken) return;
  setBusy(true);
  try {
    await fetch("/api/cancel", {
      method: "POST",
      headers: { "X-CSRF-Token": csrfToken },
      body: "",
    });
  } finally {
    setStatus("작업을 취소했습니다. 이 창을 닫아도 됩니다.", 0);
  }
});

loadSession().catch(() => {
  setBusy(true);
  setStatus("로컬 앱 연결을 확인해 주세요.", 0);
});
