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
function safeInternalUrl(value) {
  if (!value) return "";
  try {
    const url = new URL(value, window.location.origin);
    return url.origin === window.location.origin ? escapeHtml(`${url.pathname}${url.search}${url.hash}`) : "";
  } catch { return ""; }
}
function notificationItem(notification) {
  const href = safeInternalUrl(notification.target_url);
  const tag = href ? "a" : "button";
  const attrs = href
    ? `href="${href}"`
    : `type="button"`;
  return `<${tag} class="notification-item ${notification.read ? "" : "unread"}" data-notification-id="${notification.id}" ${attrs}>
    <span class="notification-dot"></span>
    <span>
      <strong>${escapeHtml(notification.title)}</strong>
      <small>${escapeHtml(notification.body)}</small>
      <time>${escapeHtml(formatDate(notification.created_at))}</time>
    </span>
  </${tag}>`;
}
function renderNotifications(unreadCount = 0) {
  const badge = document.querySelector("#notifications-badge");
  badge.hidden = unreadCount <= 0;
  badge.textContent = fa(unreadCount);
  document.querySelector("#notifications-list").innerHTML = state.notifications.length
    ? state.notifications.map(notificationItem).join("")
    : `<div class="empty-state">اعلانی ندارید.</div>`;
}
async function loadNotifications() {
  const data = await api("/api/merchant/notifications");
  state.notifications = data.notifications;
  renderNotifications(data.unread_count);
}
async function markNotificationRead(notificationId) {
  await api(`/api/merchant/notifications/${notificationId}/read`, { method: "PATCH" });
  await loadNotifications();
}
async function markAllNotificationsRead() {
  await api("/api/merchant/notifications/read-all", { method: "POST" });
  await loadNotifications();
}
function productRow(product) {
  const image = safeImage(product.image_url);
  const imageNode = image
    ? `<img class="product-image" src="${image}" alt="" loading="lazy" referrerpolicy="no-referrer">`
    : `<span class="product-placeholder">◇</span>`;
  const hasEstimate = product.market_suggested && product.effective_min && product.effective_max;
  const range = hasEstimate
    ? `<strong>${toman(product.effective_min)} تا ${toman(product.effective_max)}</strong>
       ${product.customized ? '<span class="custom-badge">شخصی</span>' : ""}`
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
      <button class="edit-range" data-edit="${product.product_id}" ${hasEstimate ? "" : "disabled"}>تنظیم</button>
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
  document.querySelector("#summary-products").textContent = fa(data.summary.products);
  document.querySelector("#summary-estimated").textContent = fa(data.summary.estimated);
  document.querySelector("#summary-customized").textContent = fa(data.summary.customized);
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
function openRange(productId) {
  const product = state.products.find(item => item.product_id === Number(productId));
  if (!product) return;
  document.querySelector("#range-product-id").value = product.product_id;
  document.querySelector("#dialog-product-title").textContent = product.title;
  document.querySelector("#range-min").value = product.effective_min;
  document.querySelector("#range-max").value = product.effective_max;
  document.querySelector("#range-error").textContent = "";
  document.querySelector("#range-dialog").showModal();
}
async function saveRange(minPrice, maxPrice) {
  const productId = document.querySelector("#range-product-id").value;
  await api(`/api/merchant/products/${productId}/range`, {
    method: "PATCH",
    body: JSON.stringify({ min_price: minPrice, max_price: maxPrice }),
  });
  document.querySelector("#range-dialog").close();
  toast(minPrice === null ? "بازه بازار بازگردانده شد." : "بازه شخصی ذخیره شد.");
  await loadDashboard();
}
document.querySelector("#sync-button").addEventListener("click", startSync);
document.querySelector("#product-filter").addEventListener("input", renderProducts);
document.querySelector("#notifications-button").addEventListener("click", () => {
  const panel = document.querySelector("#notifications-panel");
  const open = panel.hidden;
  panel.hidden = !open;
  document.querySelector("#notifications-button").setAttribute("aria-expanded", String(open));
  if (open) loadNotifications().catch(error => toast(error.message));
});
document.querySelector("#mark-notifications-read").addEventListener("click", async () => {
  try { await markAllNotificationsRead(); }
  catch (error) { toast(error.message); }
});
document.addEventListener("click", event => {
  const edit = event.target.closest("[data-edit]");
  if (edit) {
    openRange(edit.dataset.edit);
    return;
  }
  const notification = event.target.closest("[data-notification-id]");
  if (notification && notification.classList.contains("unread")) {
    const href = notification.getAttribute("href");
    if (href) event.preventDefault();
    markNotificationRead(notification.dataset.notificationId)
      .then(() => { if (href) window.location.href = href; })
      .catch(error => toast(error.message));
  }
  if (event.target.closest("[data-close]")) document.querySelector("#range-dialog").close();
  const row = event.target.closest(".product-row[data-analysis-url]");
  if (row && !event.target.closest("a, button")) {
    window.location.href = row.dataset.analysisUrl;
    return;
  }
  if (
    !event.target.closest(".notification-menu")
    && !document.querySelector("#notifications-panel").hidden
  ) {
    document.querySelector("#notifications-panel").hidden = true;
    document.querySelector("#notifications-button").setAttribute("aria-expanded", "false");
  }
});
document.querySelector("#range-form").addEventListener("submit", async event => {
  event.preventDefault();
  const min = Number(document.querySelector("#range-min").value);
  const max = Number(document.querySelector("#range-max").value);
  if (min > max) {
    document.querySelector("#range-error").textContent = "حداقل قیمت نمی‌تواند از حداکثر بیشتر باشد.";
    return;
  }
  try { await saveRange(min, max); }
  catch (error) { document.querySelector("#range-error").textContent = error.message; }
});
document.querySelector("#reset-range").addEventListener("click", async () => {
  try { await saveRange(null, null); }
  catch (error) { document.querySelector("#range-error").textContent = error.message; }
});
Promise.all([loadDashboard(), loadNotifications()]).catch(error => toast(error.message));
