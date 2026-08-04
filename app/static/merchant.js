const state = {
  products: [],
  notifications: [],
  status: "idle",
  pollTimer: null,
  notificationPollTimer: null,
  notificationLoaded: false,
  toastTimer: null,
  marketplace: "basalam",
};
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
function toast(message, options = {}) {
  const node = document.querySelector("#toast");
  const title = options.title || "اعلان";
  const kind = options.kind || "info";
  node.innerHTML = `
    <div class="toast-card ${kind}">
      <strong>${escapeHtml(title)}</strong>
      <span>${escapeHtml(message)}</span>
    </div>
  `;
  node.classList.add("show");
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    node.classList.remove("show");
    node.innerHTML = "";
  }, options.timeout || 4200);
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
function productBasalamEditUrl(product) {
  return `https://vendor.basalam.com/edit-product/${product.product_id}`;
}
async function recordButtonClick(buttonName, productId = null) {
  try {
    await fetch("/api/metrics/button-click", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        button_name: buttonName,
        product_id: productId,
        store_id: window.STORE_ID || null,
        product_url: window.location.href || null,
      }),
    });
  } catch {
    // best-effort
  }
}
function renderNotifications(unreadCount = 0) {
  const badge = document.querySelector("#notifications-badge");
  badge.hidden = unreadCount <= 0;
  badge.textContent = fa(unreadCount);
}
function maybeShowNotificationToast(nextNotifications, unreadCount) {
  if (!state.notificationLoaded || !nextNotifications.length) return;
  const previousIds = new Set(state.notifications.map(notification => notification.id));
  const newNotifications = nextNotifications.filter(notification => !previousIds.has(notification.id));
  const newestNotification = newNotifications[0] || nextNotifications.find(notification => !notification.read) || null;
  if (!newestNotification) return;
  if (unreadCount <= state.notifications.filter(notification => !notification.read).length) return;
  const body = newestNotification.body || newestNotification.title || "شما یک اعلان جدید دارید.";
  toast(body, { title: newestNotification.title || "اعلان جدید", kind: "notification" });
}

async function loadNotifications(options = {}) {
  const data = await api("/api/merchant/notifications");
  const nextNotifications = data.notifications;
  if (state.notificationLoaded) {
    maybeShowNotificationToast(nextNotifications, data.unread_count);
  } else {
    state.notificationLoaded = true;
  }
  state.notifications = nextNotifications;
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
      <a class="analysis-link" href="${analysisUrl}" onclick="recordButtonClick('merchant_analysis', ${product.product_id})">تحلیل بازار</a>
      ${product.market_suggested && state.marketplace === "basalam"
        ? `<button class="set-price-btn" onclick="setPriceInBasalam(${product.product_id}, ${product.market_suggested || 0})">تنظیم قیمت در باسلام به ${toman(product.market_suggested || 0)} تومان</button>`
        : ""}
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
function setPriceInBasalam(productId, price) {
  recordButtonClick("set_price_basalam_merchant", productId);
  const url = `https://vendor.basalam.com/edit-product/${productId}`;
  window.open(url, "_blank", "noopener");
}
async function loadDashboard() {
  const data = await api("/api/merchant/dashboard");
  state.products = data.products;
  state.marketplace = data.account.marketplace || "basalam";
  state.status = data.account.sync_status;
  const running = ["running", "queued"].includes(state.status);
  document.querySelector("#sync-state").hidden = !running;
  const priceButton = document.querySelector("#sync-button");
  const productsButton = document.querySelector("#sync-products-button");
  priceButton.disabled = running;
  productsButton.disabled = running;
  priceButton.textContent = running ? "در حال به‌روزرسانی…" : "به‌روزرسانی قیمت‌ها";
  productsButton.textContent = running ? "در حال همگام‌سازی…" : "دریافت دوباره محصولات";
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
    recordButtonClick("refresh_market_prices");
    await api("/api/merchant/refresh-prices", { method: "POST" });
    toast("به‌روزرسانی قیمت‌های بازار شروع شد.");
    await loadDashboard();
  } catch (error) {
    button.disabled = false;
    toast(error.message);
  }
}
document.querySelector("#sync-button").addEventListener("click", startSync);
document.querySelector("#sync-products-button").addEventListener("click", async function() {
  recordButtonClick("sync_products");
  const button = document.querySelector("#sync-products-button");
  button.disabled = true;
  button.textContent = "در حال به‌روزرسانی…";
  try {
    await api("/api/merchant/sync", { method: "POST" });
    toast("به‌روزرسانی محصولات شروع شد.");
    await loadDashboard();
  } catch (error) {
    button.disabled = false;
    button.textContent = "دریافت دوباره محصولات";
    toast(error.message);
  }
});
document.querySelector("#product-filter").addEventListener("input", renderProducts);
document.addEventListener("click", event => {
  const row = event.target.closest(".product-row[data-analysis-url]");
  if (row && !event.target.closest("a, button")) {
    window.location.href = row.dataset.analysisUrl;
  }
});
function startNotificationPolling() {
  if (state.notificationPollTimer) return;
  state.notificationPollTimer = window.setInterval(() => {
    loadNotifications({ showToast: true }).catch(error => toast(error.message));
  }, 10000);
}

Promise.all([loadDashboard(), loadNotifications()]).then(() => {
  startNotificationPolling();
}).catch(error => toast(error.message));
