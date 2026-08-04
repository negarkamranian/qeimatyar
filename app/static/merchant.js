const state = {
  products: [],
  notifications: [],
  status: "idle",
  pollTimer: null,
  notificationPollTimer: null,
  notificationLoaded: false,
  toastTimer: null,
  marketplace: "basalam",
  premium: null,
  expandedProducts: new Set(),
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
  let node = document.querySelector("#toast");
  if (!node) {
    node = document.createElement("div");
    node.id = "toast";
    node.setAttribute("role", "status");
    node.setAttribute("aria-live", "polite");
    document.body.appendChild(node);
  }
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

function renderPremium(premium, account = {}) {
  state.premium = premium;
  const node = document.querySelector("#premium-insights");
  if (premium.active && premium.analytics) {
    const analytics = premium.analytics;
    node.className = "premium-insights";
    node.innerHTML = `
      <div class="premium-head">
        <div><p class="eyebrow">تصویر ${fa(premium.window_days)} روزه</p><h2>اثر قیمت‌گذاری روی درآمد</h2><p>${escapeHtml(analytics.disclaimer)}</p></div>
        <span class="premium-pill">اشتراک حرفه‌ای فعال</span>
      </div>
      <div class="premium-cards">
        <article class="premium-card"><small>فرصت درآمدی سناریویی</small><strong>${toman(analytics.estimated_revenue_opportunity)} تومان</strong><span>بازآزمایی پیشنهاد فعلی روی فروش واقعی</span></article>
        <article class="premium-card"><small>درآمد فروش‌های ردیابی‌شده</small><strong>${toman(analytics.tracked_revenue)} تومان</strong><span>${fa(analytics.tracked_sales)} واحد فروش در داده قابل دریافت</span></article>
        <article class="premium-card"><small>داده رقابتی</small><strong>باز شده</strong><span>رصد ذخیره‌شده قیمت و لینک رقبا</span></article>
      </div>`;
    return;
  }

  const needsConsent = account.analytics_status === "needs_consent";
  const readyCopy = premium.teaser.has_sales_history
    ? "فروش تاریخی شما دریافت شده؛ عدد فرصت درآمدی پشت اشتراک حرفه‌ای آماده است."
    : needsConsent
      ? "با تمدید دسترسی باسلام، فروش تاریخی هم به این تحلیل اضافه می‌شود."
      : "تاریخچه فروش در همگام‌سازی‌های باسلام بررسی می‌شود.";
  const consentLink = needsConsent
    ? `<a class="premium-consent-link" data-basalam-renew href="/auth/basalam?renew=analytics">تمدید دسترسی خواندنی باسلام</a>`
    : "";
  node.className = "premium-insights premium-lock";
  node.innerHTML = `
    <div class="premium-preview" aria-hidden="true">
      <div class="premium-head"><div><p class="eyebrow">تصویر ${fa(premium.window_days)} روزه</p><h2>اثر قیمت‌گذاری روی درآمد</h2></div></div>
      <div class="premium-cards">
        <article class="premium-card"><small>فرصت درآمدی سناریویی</small><strong>۰٬۰۰۰٬۰۰۰ تومان</strong><span>بازآزمایی پیشنهاد قیمت</span></article>
        <article class="premium-card"><small>درآمد ردیابی‌شده</small><strong>۰٬۰۰۰٬۰۰۰ تومان</strong><span>فروش تاریخی غرفه</span></article>
        <article class="premium-card"><small>رقبای معتبر</small><strong>۰ فروشنده</strong><span>قیمت و لینک مستقیم</span></article>
      </div>
    </div>
    <div class="premium-lock-overlay">
      <div class="premium-lock-copy"><strong>${escapeHtml(premium.teaser.title)}</strong><p>${escapeHtml(readyCopy)}</p><button class="premium-unlock" type="button">دیدن تحلیل و قیمت رقبا</button>${consentLink}</div>
    </div>`;
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
function productFacts(product) {
  const yesNo = value => value === true ? "بله" : value === false ? "خیر" : null;
  const extra = product.raw_enrichment || {};
  const facts = [
    ["دسته‌بندی", product.category_title],
    ["وضعیت باسلام", product.status_title],
    ["بازدید / کلیک محصول", Number.isFinite(Number(product.view_count)) ? fa(product.view_count) : null],
    ["فروش ثبت‌شده باسلام", Number.isFinite(Number(product.sales_count)) ? `${fa(product.sales_count)} واحد` : null],
    ["امتیاز خریداران", product.rating !== null && product.rating !== undefined ? `${fa(product.rating)} از ۵` : null],
    ["تعداد دیدگاه", Number.isFinite(Number(product.review_count)) ? fa(product.review_count) : null],
    ["شناسه فروشنده (SKU)", product.sku],
    ["زمان آماده‌سازی", product.preparation_day ? `${fa(product.preparation_day)} روز` : null],
    ["وزن خالص", product.net_weight ? `${fa(product.net_weight)} گرم` : null],
    ["وزن بسته‌بندی", product.packaged_weight ? `${fa(product.packaged_weight)} گرم` : null],
    ["تاریخ ایجاد در باسلام", product.product_created_at ? formatDate(product.product_created_at) : null],
    ["آخرین تغییر در باسلام", product.product_updated_at ? formatDate(product.product_updated_at) : null],
    ["قابل افزودن به سبد", yesNo(extra.can_add_to_cart)],
    ["دارای تنوع", yesNo(extra.has_variation)],
    ["فروش عمده", yesNo(extra.is_wholesale)],
    ["تخفیف باسلام", extra.discount ? fa(extra.discount) : null],
    ["مقدار واحد", extra.unit_quantity ? `${fa(extra.unit_quantity)} ${extra.unit_type || ""}`.trim() : null],
  ].filter(([, value]) => value !== null && value !== undefined && value !== "");
  if (!facts.length) {
    return `<div class="product-details-empty"><strong>اطلاعات تکمیلی هنوز دریافت نشده است.</strong><span>«دریافت دوباره محصولات» را بزنید تا بازدید، فروش، دسته‌بندی و امتیاز محصول از باسلام خوانده شود.</span></div>`;
  }
  return `<div class="product-facts">${facts.map(([label, value]) => `
    <div class="product-fact"><small>${escapeHtml(label)}</small><strong>${escapeHtml(value)}</strong></div>
  `).join("")}</div>`;
}

function basalamPriceHistory(product) {
  const points = (product.basalam_price_history || []).slice().reverse().slice(0, 12);
  if (!points.length) {
    return `<div class="basalam-history"><div class="details-subhead"><strong>تاریخچه قیمت باسلام</strong><small>API باسلام هنوز تغییر قیمتی برای این محصول برنگردانده است.</small></div></div>`;
  }
  return `<div class="basalam-history">
    <div class="details-subhead"><strong>تاریخچه قیمت باسلام</strong><small>${fa(points.length)} تغییر اخیر دریافت‌شده از API باسلام</small></div>
    <div class="price-history-list">${points.map(point => `
      <div class="price-history-item"><time>${escapeHtml(formatDate(point.changed_at))}</time><strong>${toman(point.price)} تومان</strong>${point.discounted_price ? `<small>با تخفیف: ${toman(point.discounted_price)} تومان</small>` : ""}</div>
    `).join("")}</div>
  </div>`;
}

function productCard(product) {
  const image = safeImage(product.image_url);
  const imageNode = image
    ? `<img class="product-image" src="${image}" alt="" loading="lazy" referrerpolicy="no-referrer">`
    : `<span class="product-placeholder">◇</span>`;
  const hasEstimate = product.market_suggested && product.effective_min && product.effective_max;
  const range = hasEstimate
    ? `<strong>${toman(product.market_low)} تا ${toman(product.market_high)}</strong>`
    : `<strong class="estimate-error">${escapeHtml(product.estimate_error || "در انتظار تحلیل")}</strong>`;
  const analysisUrl = escapeHtml(productAnalysisUrl(product));
  const expanded = state.expandedProducts.has(product.product_id);
  return `<article class="product-card" data-title="${escapeHtml(product.title.toLowerCase())}">
    <div class="product-row">
    <button class="product-identity" type="button" data-product-toggle="${product.product_id}" aria-expanded="${expanded}">${imageNode}<span>
      <strong title="${escapeHtml(product.title)}">${escapeHtml(product.title)}</strong>
      <small>موجودی ${fa(product.stock)}${product.category_title ? ` · ${escapeHtml(product.category_title)}` : ""}</small>
    </span></button>
    <div class="price-block"><small>قیمت فعلی</small><strong>${toman(product.current_price)} تومان</strong></div>
    <div class="range-block"><small>بازه پیشنهادی</small>${range}</div>
    <div class="product-actions">
      <button class="details-toggle" type="button" data-product-toggle="${product.product_id}" aria-expanded="${expanded}">${expanded ? "بستن جزئیات" : "جزئیات محصول"}</button>
      <a class="analysis-link" href="${analysisUrl}" onclick="recordButtonClick('merchant_analysis', ${product.product_id})">تحلیل بازار</a>
      ${product.market_suggested && state.marketplace === "basalam"
        ? `<button class="set-price-btn" onclick="setPriceInBasalam(${product.product_id}, ${product.market_suggested || 0})">تنظیم قیمت در باسلام به ${toman(product.market_suggested || 0)} تومان</button>`
        : ""}
    </div>
    </div>
    <section class="product-details" id="product-details-${product.product_id}" ${expanded ? "" : "hidden"}>
      <div class="product-details-title"><div><small>داده‌های دریافت‌شده از API باسلام</small><strong>${escapeHtml(product.title)}</strong></div>${product.basalam_url ? `<a href="${escapeHtml(product.basalam_url)}" target="_blank" rel="noopener noreferrer">صفحه محصول در باسلام ↗</a>` : ""}</div>
      ${productFacts(product)}
      ${basalamPriceHistory(product)}
    </section>
  </article>`;
}
function renderProducts() {
  const query = document.querySelector("#product-filter").value.trim().toLowerCase();
  const products = state.products.filter(product => product.title.toLowerCase().includes(query));
  document.querySelector("#products-list").innerHTML = products.length
    ? products.map(productCard).join("")
    : `<div class="empty-state">${state.products.length ? "محصولی با این نام نیست." : "هنوز محصولی دریافت نشده است."}</div>`;
}

function toggleProductDetails(productId, forceOpen = null) {
  const numericId = Number(productId);
  const shouldOpen = forceOpen === null ? !state.expandedProducts.has(numericId) : forceOpen;
  if (shouldOpen) state.expandedProducts.add(numericId);
  else state.expandedProducts.delete(numericId);
  renderProducts();
  if (shouldOpen) {
    window.setTimeout(() => {
      document.querySelector(`#product-details-${numericId}`)?.scrollIntoView({ behavior: "smooth", block: "nearest" });
    }, 0);
  }
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
  renderPremium(data.premium, data.account);
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
  const detailsButton = event.target.closest("[data-product-toggle]");
  if (detailsButton) {
    toggleProductDetails(detailsButton.dataset.productToggle);
    return;
  }
  const renewLink = event.target.closest("[data-basalam-renew]");
  if (renewLink) {
    recordButtonClick("basalam_analytics_reconsent");
    renewLink.textContent = "در حال انتقال به باسلام…";
    renewLink.setAttribute("aria-busy", "true");
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
