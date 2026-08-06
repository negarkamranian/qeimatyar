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
        <div><p class="eyebrow">تصویر ${fa(premium.window_days)} روزه فروش</p><h2>اثر قیمت‌گذاری روی درآمد</h2><p>${escapeHtml(analytics.disclaimer)}</p></div>
        <span class="premium-pill">اشتراک حرفه‌ای فعال</span>
      </div>
      <div class="premium-cards">
        <article class="premium-card"><small>فرصت درآمدی برآوردشده</small><strong>${toman(analytics.estimated_revenue_opportunity)} تومان</strong><span>اگر پیشنهاد فعلی روی فروش‌های گذشته اعمال می‌شد</span></article>
        <article class="premium-card"><small>درآمد ثبت‌شده</small><strong>${toman(analytics.tracked_revenue)} تومان</strong><span>بر پایه ${fa(analytics.tracked_sales)} فروش دریافت‌شده از باسلام</span></article>
        <article class="premium-card"><small>داده رقبا</small><strong>فعال</strong><span>قیمت، لینک و روند تغییرات رقبا</span></article>
      </div>`;
    return;
  }

  const needsConsent = account.analytics_status === "needs_consent";
  const analyticsPending = ["pending", "running"].includes(account.analytics_status);
  const readyCopy = needsConsent
    ? "برای افزودن فروش‌های گذشته به تحلیل، دسترسی فقط‌خواندنی سفارش‌های باسلام را یک‌بار تأیید کنید."
    : analyticsPending
      ? "دسترسی فروش در حال بررسی است؛ نتیجه همین‌جا و بدون اقدام دیگری نمایش داده می‌شود."
      : premium.teaser.has_sales_history
        ? "تاریخچه فروش دریافت شده و تحلیل درآمدی شما آماده فعال‌سازی است."
        : "با ثبت داده بیشتر، رابطه قیمت، فروش و درآمد دقیق‌تر می‌شود.";
  const consentLink = needsConsent
    ? `<a class="premium-consent-link" data-basalam-renew href="/auth/basalam?renew=analytics">تأیید دسترسی فروش باسلام</a>`
    : "";
  node.className = "premium-insights premium-lock";
  node.innerHTML = `
    <div class="premium-lock-content">
      <div class="premium-lock-copy">
        <span class="analytics-lock-icon">⌁</span>
        <strong>${escapeHtml(premium.teaser.title)}</strong>
        <p>${escapeHtml(readyCopy)}</p>
        <div class="premium-actions">
          ${consentLink}
          <span class="premium-plan-note">این امکانات در اشتراک حرفه‌ای باز می‌شوند</span>
        </div>
      </div>
      <div class="premium-feature-list" aria-label="امکانات تحلیل حرفه‌ای">
        <div class="premium-feature"><i>↗</i><div><span>فرصت درآمدی</span><small>برآورد بر پایه فروش واقعی</small></div></div>
        <div class="premium-feature"><i>≋</i><div><span>روند قیمت و فروش</span><small>نمای ${fa(premium.window_days)} روزه هر محصول</small></div></div>
        <div class="premium-feature"><i>◎</i><div><span>تحلیل رقبا</span><small>قیمت و لینک فروشندگان مشابه</small></div></div>
      </div>
    </div>`;
}

function productPricingPosition(product) {
  const current = Number(product.current_price) || 0;
  const suggested = Number(product.market_suggested) || 0;
  const low = Number(product.effective_min || product.market_low) || 0;
  const high = Number(product.effective_max || product.market_high) || 0;
  if (!current || !suggested || !low || !high) return null;
  const deltaPct = ((current - suggested) / Math.max(suggested, 1)) * 100;
  return { current, suggested, low, high, deltaPct };
}

function topProductNames(products) {
  return products.slice(0, 2).map(product => product.title).join("، ");
}

function firstProductAction(products, label, metricName) {
  const product = products[0];
  if (!product) return "";
  return `<a class="strategy-action" href="${escapeHtml(productAnalysisUrl(product))}" onclick="recordButtonClick('${metricName}', ${product.product_id})">${escapeHtml(label)}</a>`;
}

function strategyCard({ tone, label, title, body, metric, products = [], action = "" }) {
  const productLine = products.length
    ? `<small class="strategy-products">${escapeHtml(topProductNames(products))}</small>`
    : "";
  return `<article class="strategy-card ${tone}">
    <span class="strategy-label">${escapeHtml(label)}</span>
    <div>
      <h3>${escapeHtml(title)}</h3>
      <p>${escapeHtml(body)}</p>
      ${productLine}
    </div>
    <footer><strong>${escapeHtml(metric)}</strong>${action}</footer>
  </article>`;
}

function buildStrategicInsights(products, summary = {}) {
  const priced = products
    .map(product => ({ product, position: productPricingPosition(product) }))
    .filter(item => item.position);
  const overpriced = priced
    .filter(({ position }) => position.current > position.high || position.deltaPct >= 12)
    .sort((a, b) => b.position.deltaPct - a.position.deltaPct)
    .map(item => item.product);
  const underpriced = priced
    .filter(({ position }) => position.current < position.low || position.deltaPct <= -8)
    .sort((a, b) => a.position.deltaPct - b.position.deltaPct)
    .map(item => item.product);
  const missingEstimate = products
    .filter(product => !product.market_suggested || product.estimate_error)
    .sort((a, b) => Number(b.stock || 0) - Number(a.stock || 0));
  const weakPresentation = products
    .filter(product => Number(product.stock || 0) > 0 && Number(product.view_count || 0) === 0 && product.market_suggested)
    .slice(0, 6);
  const readyCount = Number(summary.estimated) || priced.length;
  const cards = [];

  if (underpriced.length) {
    cards.push(strategyCard({
      tone: "profit",
      label: "فرصت سود",
      title: "چند محصول ظرفیت افزایش قیمت دارند",
      body: "این محصولات پایین‌تر از بازه یا پیشنهاد بازار هستند؛ می‌شود بدون خروج از محدوده رقابتی سود را بهتر کرد.",
      metric: `${fa(underpriced.length)} محصول`,
      products: underpriced,
      action: firstProductAction(underpriced, "دیدن اولین فرصت", "strategy_profit"),
    }));
  }

  if (overpriced.length) {
    cards.push(strategyCard({
      tone: "risk",
      label: "ریسک فروش کند",
      title: "قیمت چند محصول از بازار فاصله گرفته",
      body: "این محصولات نسبت به پیشنهاد دقیقه یا سقف بازار بالاترند و احتمالا برای فروش سریع‌تر نیاز به اصلاح دارند.",
      metric: `${fa(overpriced.length)} محصول`,
      products: overpriced,
      action: firstProductAction(overpriced, "بررسی قیمت", "strategy_overpriced"),
    }));
  }

  if (missingEstimate.length) {
    cards.push(strategyCard({
      tone: "coverage",
      label: "تکمیل رصد",
      title: "چند محصول هنوز تحلیل قابل اتکا ندارند",
      body: "برای اینکه پیشنهادهای فروش دقیق‌تر شوند، ابتدا محصولات بدون برآورد را دوباره با بازار همگام کنید.",
      metric: `${fa(missingEstimate.length)} محصول`,
      products: missingEstimate,
      action: `<button class="strategy-action" type="button" data-strategy-action="sync-prices">شروع رصد</button>`,
    }));
  }

  if (weakPresentation.length) {
    cards.push(strategyCard({
      tone: "growth",
      label: "رشد فروش",
      title: "موجودی دارید اما نشانه تقاضا کم است",
      body: "برای این محصولات قیمت موجود است، اما بازدید یا فروش ثبت‌شده کم دیده می‌شود؛ عنوان، عکس و تحلیل بازار را اولویت بدهید.",
      metric: `${fa(weakPresentation.length)} محصول`,
      products: weakPresentation,
      action: firstProductAction(weakPresentation, "بازبینی محصول", "strategy_growth"),
    }));
  }

  cards.push(strategyCard({
    tone: "watch",
    label: "پایش روزانه",
    title: "پیشنهادهای آماده را هر روز مرور کنید",
    body: "با تغییر قیمت رقبا، بهترین تصمیم برای هر محصول عوض می‌شود؛ محصولات آماده تحلیل را از همین صفحه دنبال کنید.",
    metric: `${fa(readyCount)} تحلیل آماده`,
    products: priced.map(item => item.product),
    action: `<button class="strategy-action" type="button" data-strategy-action="focus-products">دیدن لیست</button>`,
  }));

  return cards.slice(0, 3);
}

function renderStrategicInsights(products, summary = {}) {
  const node = document.querySelector("#strategic-insights");
  if (!node) return;
  if (!products.length) {
    node.className = "strategic-insights empty";
    node.innerHTML = `
      <div class="strategy-empty"><strong>هنوز پیشنهادی ساخته نشده است.</strong><p>محصولات را دریافت کنید تا دقیقه مهم‌ترین اقدام‌های امروز را اینجا بچیند.</p></div>`;
    return;
  }
  node.className = "strategic-insights";
  node.innerHTML = `
    <div class="strategy-grid">
      ${buildStrategicInsights(products, summary).join("")}
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
    ? `<strong>${toman(product.market_suggested)} تومان</strong><span class="market-range">بازه بازار: ${toman(product.market_low)} تا ${toman(product.market_high)}</span>`
    : `<strong class="estimate-error">${escapeHtml(product.estimate_error || "در انتظار تحلیل")}</strong>`;
  const position = productPricingPosition(product);
  let priceSignal = "";
  if (position) {
    const distance = Math.abs(position.deltaPct);
    if (distance < 5) {
      priceSignal = `<span class="price-signal good">در محدوده مناسب</span>`;
    } else if (position.deltaPct > 0) {
      priceSignal = `<span class="price-signal lower">پیشنهاد کاهش ${fa(Math.round(distance))}٪</span>`;
    } else {
      priceSignal = `<span class="price-signal raise">ظرفیت افزایش ${fa(Math.round(distance))}٪</span>`;
    }
  }
  const analysisUrl = escapeHtml(productAnalysisUrl(product));
  const expanded = state.expandedProducts.has(product.product_id);
  return `<article class="product-card" data-product-id="${product.product_id}" data-title="${escapeHtml(product.title.toLowerCase())}">
    <div class="product-row">
    <button class="product-identity" type="button" data-product-toggle="${product.product_id}" aria-expanded="${expanded}">${imageNode}<span>
      <strong title="${escapeHtml(product.title)}">${escapeHtml(product.title)}</strong>
      <small>موجودی ${fa(product.stock)}${product.category_title ? ` · ${escapeHtml(product.category_title)}` : ""}</small>
    </span></button>
    <div class="price-block"><small>قیمت فعلی</small><strong>${toman(product.current_price)} تومان</strong>${priceSignal}</div>
    <div class="range-block"><small>پیشنهاد دقیقه</small>${range}</div>
    <div class="product-actions">
      <a class="analysis-link" href="${analysisUrl}" onclick="recordButtonClick('merchant_analysis', ${product.product_id})">تحلیل بازار</a>
      ${product.market_suggested && state.marketplace === "basalam"
        ? `<button class="set-price-btn" onclick="setPriceInBasalam(${product.product_id}, ${product.market_suggested || 0})">اعمال در باسلام ↗</button>`
        : ""}
      <button class="details-toggle" type="button" data-product-toggle="${product.product_id}" aria-expanded="${expanded}">${expanded ? "بستن" : "جزئیات"}</button>
    </div>
    </div>
    <section class="product-details" id="product-details-${product.product_id}" ${expanded ? "" : "hidden"}>
      <div class="product-details-title"><div><small>جزئیات محصول</small><strong>${escapeHtml(product.title)}</strong></div><div class="product-details-links"><a class="elasticity-link" href="/merchant/products/${product.product_id}/elasticity" onclick="recordButtonClick('merchant_elasticity', ${product.product_id})">کشش قیمت</a>${product.basalam_url ? `<a href="${escapeHtml(product.basalam_url)}" target="_blank" rel="noopener noreferrer">صفحه باسلام ↗</a>` : ""}</div></div>
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

function renderOverview(data) {
  const total = Number(data.summary.products) || data.products.length;
  const ready = Number(data.summary.estimated) || 0;
  const status = data.account.analytics_status || "pending";
  const analyticsLabels = {
    ready: ["فعال", "تاریخچه فروش به‌روز است"],
    partial: ["فعال", "بخشی از داده‌ها دریافت شده"],
    running: ["در حال دریافت", "داده‌های فروش در حال همگام‌سازی است"],
    pending: ["در حال بررسی", "وضعیت دسترسی در حال بررسی است"],
    needs_consent: ["نیاز به تأیید", "دسترسی فروش باسلام را تأیید کنید"],
  };
  const analytics = state.marketplace === "digikala"
    ? ["ناموجود", "اتصال فعلی داده تاریخچه فروش ندارد"]
    : (analyticsLabels[status] || ["در حال بررسی", "وضعیت دسترسی در حال بررسی است"]);
  document.querySelector("#summary-products").textContent = fa(total);
  document.querySelector("#summary-ready").textContent = fa(ready);
  document.querySelector("#summary-review").textContent = fa(Math.max(0, total - ready));
  document.querySelector("#summary-analytics").textContent = analytics[0];
  document.querySelector("#summary-analytics-copy").textContent = analytics[1];
}

async function loadDashboard() {
  const data = await api("/api/merchant/dashboard");
  state.products = data.products;
  state.marketplace = data.account.marketplace || "basalam";
  renderOverview(data);
  renderStrategicInsights(state.products, data.summary);
  renderPremium(data.premium, data.account);
  state.status = data.account.sync_status;
  const running = ["running", "queued"].includes(state.status);
  document.querySelector("#sync-state").hidden = !running;
  const priceButton = document.querySelector("#sync-button");
  const productsButton = document.querySelector("#sync-products-button");
  priceButton.disabled = running;
  productsButton.disabled = running;
  priceButton.innerHTML = running ? "در حال به‌روزرسانی…" : `<svg viewBox="0 0 24 24" aria-hidden="true"><path d="M20 6v5h-5M4 18v-5h5M6.1 9A7 7 0 0 1 18 6.5L20 11M4 13l2 4.5A7 7 0 0 0 17.9 15"></path></svg>به‌روزرسانی تحلیل‌ها`;
  productsButton.textContent = running ? "در حال همگام‌سازی…" : "دریافت محصولات";
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
    button.textContent = "دریافت محصولات";
    toast(error.message);
  }
});
document.querySelector("#product-filter").addEventListener("input", renderProducts);
document.addEventListener("click", event => {
  const scrollProducts = event.target.closest("[data-scroll-products]");
  if (scrollProducts) {
    document.querySelector("#products")?.scrollIntoView({ behavior: "smooth", block: "start" });
    return;
  }
  const strategyAction = event.target.closest("[data-strategy-action]");
  if (strategyAction) {
    const action = strategyAction.dataset.strategyAction;
    if (action === "sync-prices") {
      recordButtonClick("strategy_sync_prices");
      document.querySelector("#sync-button")?.click();
    }
    if (action === "focus-products") {
      recordButtonClick("strategy_focus_products");
      document.querySelector(".products-section")?.scrollIntoView({ behavior: "smooth", block: "start" });
    }
    return;
  }
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

const dashboardSections = [...document.querySelectorAll("main > section[id]")];
const dashboardLinks = [...document.querySelectorAll(".merchant-nav a")];
if ("IntersectionObserver" in window) {
  const sectionObserver = new IntersectionObserver(entries => {
    const visible = entries.filter(entry => entry.isIntersecting).sort((a, b) => b.intersectionRatio - a.intersectionRatio)[0];
    if (!visible) return;
    dashboardLinks.forEach(link => link.classList.toggle("active", link.hash === `#${visible.target.id}`));
  }, { rootMargin: "-20% 0px -65%", threshold: [0, .25, .6] });
  dashboardSections.forEach(section => sectionObserver.observe(section));
}
function startNotificationPolling() {
  if (state.notificationPollTimer) return;
  state.notificationPollTimer = window.setInterval(() => {
    loadNotifications({ showToast: true }).catch(error => toast(error.message));
  }, 10000);
}

Promise.all([loadDashboard(), loadNotifications()]).then(() => {
  if (window.location.hash) {
    const target = document.querySelector(window.location.hash);
    window.setTimeout(() => target?.scrollIntoView({ block: "start" }), 0);
  }
  startNotificationPolling();
}).catch(error => toast(error.message));
