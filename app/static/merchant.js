const state = { products: [], notifications: [], status: "idle", pollTimer: null };
const toman = value => new Intl.NumberFormat("fa-IR").format(value || 0);
const fa = value => new Intl.NumberFormat("fa-IR").format(value || 0);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}
function safeImage(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? escapeHtml(url.href) : "";
  } catch { return ""; }
}
function toast(message) {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2600);
}
async function api(url, options = {}) {
  const response = await fetch(url, {
    headers: { "Content-Type": "application/json", ...(options.headers || {}) },
    ...options,
  });
  if (response.status === 401) {
    window.location.href = "/login";
    throw new Error("نشست شما پایان یافته است.");
  }
  const body = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(body.detail || "درخواست ناموفق بود.");
  return body;
}
function formatDate(value) {
  if (!value) return "هنوز به‌روزرسانی نشده";
  return new Intl.DateTimeFormat("fa-IR", {
    dateStyle: "medium", timeStyle: "short",
  }).format(new Date(value));
}
function productAnalysisUrl(product) {
  const params = new URLSearchParams({
    q: product.title,
    from: "merchant",
    product_id: String(product.product_id),
  });
  return `/?${params.toString()}`;
}
function renderNotifications(unreadCount = 0) {
  const badge = document.querySelector("#notifications-badge");
  badge.hidden = unreadCount <= 0;
  badge.textContent = fa(unreadCount);
}
async function loadNotifications() {
  const data = await api("/api/merchant/notifications");
  state.notifications = data.notifications;
  renderNotifications(data.unread_count);
}
function productRow(product) {
  const image = safeImage(product.image_url);
  const imageNode = image
    ? `<img class="product-image" src="${image}" alt="" loading="lazy" referrerpolicy="no-referrer">`
    : `<span class="product-placeholder">◇</span>`;
  const hasEstimate = product.market_suggested && product.effective_min && product.effective_max;
  const range = hasEstimate
    ? `<strong>${toman(product.market_low)} تا ${toman(product.market_high)}</strong>`
    : `<strong class="estimate-error">${escapeHtml(product.estimate_error || "در انتظار تحلیل")}</strong>`;
  const analysisUrl = escapeHtml(productAnalysisUrl(product));
  return `<article class="product-row" data-title="${escapeHtml(product.title.toLowerCase())}" data-analysis-url="${analysisUrl}">
    <a class="product-identity" href="${analysisUrl}">${imageNode}<span>
      <strong title="${escapeHtml(product.title)}">${escapeHtml(product.title)}</strong>
      <small>موجودی ${fa(product.stock)} · مشاهده تحلیل بازار</small>
    </span></a>
    <div class="price-block"><small>قیمت فعلی</small><strong>${toman(product.current_price)} تومان</strong></div>
    <div class="range-block"><small>بازه پیشنهادی</small>${range}</div>
    <div class="product-actions">
      <a class="analysis-link" href="${analysisUrl}">تحلیل بازار</a>
    </div>
  </article>`;
}
function renderProducts() {
  const query = document.querySelector("#product-filter").value.trim().toLowerCase();
  const products = state.products.filter(product => product.title.toLowerCase().includes(query));
  document.querySelector("#products-list").innerHTML = products.length
    ? products.map(productRow).join("")
    : `<div class="empty-state">${state.products.length ? "محصولی با این نام نیست." : "هنوز محصولی دریافت نشده است."}</div>`;
}
async function loadDashboard() {
  const data = await api("/api/merchant/dashboard");
  state.products = data.products;
  state.status = data.account.sync_status;
  const running = ["running", "queued"].includes(state.status);
  document.querySelector("#sync-state").hidden = !running;
  document.querySelector("#sync-button").disabled = running;
  document.querySelector("#sync-button").textContent = running ? "در حال به‌روزرسانی…" : "به‌روزرسانی قیمت‌ها";
  document.querySelector("#sync-caption").textContent = data.account.sync_error
    || `آخرین به‌روزرسانی: ${formatDate(data.account.last_synced_at)}`;
  renderProducts();
  clearTimeout(state.pollTimer);
  if (running) state.pollTimer = setTimeout(loadDashboard, 5000);
}
async function startSync() {
  const button = document.querySelector("#sync-button");
  button.disabled = true;
  try {
    await api("/api/merchant/sync", { method: "POST" });
    toast("به‌روزرسانی شروع شد.");
    await loadDashboard();
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
}
document.querySelector("#sync-button").addEventListener("click", startSync);
document.querySelector("#product-filter").addEventListener("input", renderProducts);
document.addEventListener("click", event => {
  const row = event.target.closest(".product-row[data-analysis-url]");
  if (row && !event.target.closest("a, button")) {
    window.location.href = row.dataset.analysisUrl;
  }
});
Promise.all([loadDashboard(), loadNotifications()]).catch(error => toast(error.message));
