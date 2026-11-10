const byId = (id) => document.getElementById(id);
const cameraInput = byId("camera-input");
const libraryInput = byId("library-input");
const uploadStatus = byId("upload-status");
const reviewCard = byId("review-card");
const receiptForm = byId("receipt-form");
const itemsEditor = byId("items-editor");
let activeStageId = null;
let previewUrl = null;
let isBusy = false;

function showMessage(node, message, kind = "") {
  node.textContent = message;
  node.hidden = !message;
  node.classList.toggle("is-error", kind === "error");
}

function renderItems(items) {
  itemsEditor.replaceChildren();
  if (!items.length) {
    const empty = document.createElement("p");
    empty.className = "items-empty";
    empty.textContent = "No items detected. Add one if the receipt lists purchased items.";
    itemsEditor.append(empty);
    return;
  }
  items.forEach(addItemRow);
}

function addItemRow(item = {}) {
  itemsEditor.querySelector(".items-empty")?.remove();
  const row = document.createElement("div");
  row.className = "item-row";
  for (const [field, title, maxLength] of [["description", "Description", 300], ["category", "Category", 100]]) {
    const label = document.createElement("label");
    label.className = "item-field";
    const caption = document.createElement("span");
    caption.textContent = title;
    const input = document.createElement("input");
    input.type = "text";
    input.maxLength = maxLength;
    input.autocomplete = "off";
    input.value = item[field] || "";
    input.dataset.itemField = field;
    input.placeholder = `Unknown ${field}`;
    label.append(caption, input);
    row.append(label);
  }
  const remove = document.createElement("button");
  remove.className = "quiet-button item-remove";
  remove.type = "button";
  remove.textContent = "Remove";
  remove.addEventListener("click", () => {
    row.remove();
    if (!itemsEditor.querySelector(".item-row")) renderItems([]);
  });
  row.append(remove);
  itemsEditor.append(row);
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  headers.set("X-Requested-With", "receipt-app");
  const response = await fetch(path, { ...options, headers, credentials: "same-origin" });
  if (response.status === 401) {
    window.location.assign("/");
    throw new Error("Your session expired. Sign in again.");
  }
  if (!response.ok) {
    let message = `Request failed (${response.status}).`;
    try {
      const body = await response.json();
      if (body.detail) message = body.detail;
    } catch { /* Keep the status message. */ }
    throw new Error(message);
  }
  return response;
}

async function chooseImage(file) {
  if (!file || isBusy) return;
  resetReview(false);
  isBusy = true;
  showMessage(uploadStatus, "Reading receipt… This can take a moment.");
  const cameraButton = byId("camera-button");
  const libraryButton = byId("library-button");
  cameraButton.disabled = libraryButton.disabled = true;
  previewUrl = URL.createObjectURL(file);
  try {
    const form = new FormData();
    form.append("file", file, file.name || "receipt-image");
    const response = await api("/api/receipts/extract", { method: "POST", body: form });
    const result = await response.json();
    activeStageId = result.staged_id;
    byId("receipt-preview").src = previewUrl;
    byId("review-filename").textContent = result.original_filename;
    for (const field of ["merchant", "receipt_date", "receipt_time", "subtotal", "tax", "total", "payment", "change", "currency"]) {
      byId(field).value = result.fields[field] || "";
    }
    renderItems(result.items || []);
    reviewCard.hidden = false;
    showMessage(uploadStatus, "Suggestions are ready. Check them against the receipt.");
    reviewCard.scrollIntoView({ behavior: "smooth", block: "start" });
  } catch (error) {
    showMessage(uploadStatus, error.message, "error");
    if (previewUrl) URL.revokeObjectURL(previewUrl);
    previewUrl = null;
  } finally {
    isBusy = false;
    cameraButton.disabled = libraryButton.disabled = false;
    cameraInput.value = "";
    libraryInput.value = "";
  }
}

function resetReview(discardStage = true) {
  if (discardStage && activeStageId) {
    api(`/api/receipts/staged/${activeStageId}`, { method: "DELETE" }).catch(() => {});
  }
  activeStageId = null;
  reviewCard.hidden = true;
  receiptForm.reset();
  renderItems([]);
  byId("receipt-preview").removeAttribute("src");
  if (previewUrl) URL.revokeObjectURL(previewUrl);
  previewUrl = null;
  showMessage(byId("save-status"), "");
}

function formatDate(value) {
  if (!value) return "Date unknown";
  const [year, month, day] = value.split("-").map(Number);
  return new Intl.DateTimeFormat(undefined, { year: "numeric", month: "short", day: "numeric", timeZone: "UTC" }).format(new Date(Date.UTC(year, month - 1, day)));
}

function makeReceiptCard(receipt) {
  const article = document.createElement("article");
  article.className = "receipt-row";
  const icon = document.createElement("div");
  icon.className = "receipt-thumb";
  icon.setAttribute("aria-hidden", "true");
  icon.textContent = "▤";
  const details = document.createElement("div");
  details.className = "receipt-details";
  const merchant = document.createElement("h3");
  merchant.textContent = receipt.merchant || receipt.original_filename || "Receipt";
  const meta = document.createElement("p");
  const archivedFilename = receipt.archived_filename || receipt.original_filename;
  meta.textContent = [formatDate(receipt.receipt_date), archivedFilename].filter(Boolean).join(" · ");
  details.append(merchant, meta);
  const amount = document.createElement("div");
  amount.className = "receipt-amount";
  amount.textContent = receipt.total ? `${receipt.currency ? `${receipt.currency} ` : ""}${receipt.total}` : "Total unknown";
  const link = document.createElement("a");
  link.className = "download-link";
  link.href = `/api/receipts/${encodeURIComponent(receipt.id)}/file`;
  link.download = archivedFilename;
  link.setAttribute("aria-label", `Download archived image: ${archivedFilename}`);
  link.textContent = "↓";
  article.append(icon, details, amount, link);
  const summaryFields = [
    ["Subtotal", receipt.subtotal], ["Tax", receipt.tax], ["Total", receipt.total],
    ["Payment", receipt.payment], ["Change / cash back", receipt.change],
  ].filter(([, value]) => value);
  if (summaryFields.length || receipt.items?.length) {
    const itemDetails = document.createElement("details");
    itemDetails.className = "receipt-items-summary";
    const summary = document.createElement("summary");
    const itemCount = receipt.items?.length || 0;
    summary.textContent = [
      summaryFields.length ? "Amounts" : "",
      itemCount ? `${itemCount} item${itemCount === 1 ? "" : "s"}` : "",
    ].filter(Boolean).join(" · ");
    const itemList = document.createElement("ul");
    for (const [label, value] of summaryFields) {
      const entry = document.createElement("li");
      entry.textContent = `${label}: ${receipt.currency ? `${receipt.currency} ` : ""}${value}`;
      itemList.append(entry);
    }
    for (const item of receipt.items || []) {
      const entry = document.createElement("li");
      entry.textContent = [item.description || "Description unknown", item.category || "Category unknown"].join(" · ");
      itemList.append(entry);
    }
    itemDetails.append(summary, itemList);
    article.append(itemDetails);
  }
  return article;
}

async function loadReceipts() {
  const list = byId("receipt-list");
  try {
    const response = await api("/api/receipts");
    const { receipts } = await response.json();
    byId("receipt-count").textContent = String(receipts.length);
    list.replaceChildren();
    if (!receipts.length) {
      const empty = document.createElement("div");
      empty.className = "empty-state";
      const icon = document.createElement("span");
      icon.className = "empty-icon";
      icon.setAttribute("aria-hidden", "true");
      icon.textContent = "▤";
      const title = document.createElement("p");
      title.textContent = "No receipts yet.";
      const note = document.createElement("span");
      note.textContent = "Your saved receipts will appear here.";
      empty.append(icon, title, note);
      list.append(empty);
    } else {
      receipts.forEach((receipt) => list.append(makeReceiptCard(receipt)));
    }
  } catch (error) {
    list.textContent = error.message;
  }
}

byId("camera-button").addEventListener("click", () => cameraInput.click());
byId("library-button").addEventListener("click", () => libraryInput.click());
cameraInput.addEventListener("change", () => chooseImage(cameraInput.files[0]));
libraryInput.addEventListener("change", () => chooseImage(libraryInput.files[0]));
byId("discard-button").addEventListener("click", () => {
  resetReview();
  showMessage(uploadStatus, "Upload discarded.");
});
byId("add-item-button").addEventListener("click", () => addItemRow());
byId("refresh-button").addEventListener("click", loadReceipts);

receiptForm.addEventListener("submit", async (event) => {
  event.preventDefault();
  if (!activeStageId || isBusy) return;
  isBusy = true;
  const saveButton = byId("save-button");
  saveButton.disabled = true;
  showMessage(byId("save-status"), "Saving receipt…");
  const body = Object.fromEntries(new FormData(receiptForm).entries());
  body.staged_id = activeStageId;
  body.items = [...itemsEditor.querySelectorAll(".item-row")].map((row) => ({
    description: row.querySelector('[data-item-field="description"]').value,
    category: row.querySelector('[data-item-field="category"]').value,
  })).filter((item) => item.description.trim() || item.category.trim());
  try {
    await api("/api/receipts", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body) });
    resetReview(false);
    showMessage(uploadStatus, "Receipt saved to your archive.");
    await loadReceipts();
    byId("archive-title").scrollIntoView({ behavior: "smooth", block: "center" });
  } catch (error) {
    showMessage(byId("save-status"), error.message, "error");
  } finally {
    isBusy = false;
    saveButton.disabled = false;
  }
});

byId("logout-button").addEventListener("click", async () => {
  try {
    await api("/logout", { method: "POST" });
    window.location.assign("/");
  } catch (error) {
    showMessage(uploadStatus, error.message, "error");
  }
});

loadReceipts();
if ("serviceWorker" in navigator) navigator.serviceWorker.register("/service-worker.js").catch(() => {});
