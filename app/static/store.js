const state = {
  storeId: null,
  products: [],
  status: "idle",
};

const toman = value => new Intl.NumberFormat("fa-IR").format(Math.round(value || 0));
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
}

function formatDate(value) {
  if (!value) return "—";
  return new Intl.DateTimeFormat("fa-IR", {
    dateStyle: "medium", timeStyle: "short",
  }).format(new Date(value));
}

function recordButtonClick(buttonName, productId) {
  fetch("/api/metrics/button-click", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      button_name: buttonName,
      product_id: productId,
      store_id: state.storeId,
    }),
  }).catch(() => {});
}

function productRow(product) {
  const image = safeImage(product.image_url);
  const imageNode = image
    ? `<img class="product-image" src="${image}" alt="" loading="lazy" referrerpolicy="no-referrer">`
    : `<span class="product-placeholder">◇</span>`;
  const analysis = product.analysis;
  const hasEstimate = analysis && analysis.recommended;
  const range = hasEstimate
    ? `<strong>${toman(analysis.range.low)} تا ${toman(analysis.range.high)}</strong>`
    : `<strong class="estimate-error">${escapeHtml(analysis?.estimate_error || "تحلیل نشده")}</strong>`;
  const analysisUrl = `/?q=${encodeURIComponent(product.url)}&from=store&store_id=${encodeURIComponent(state.storeId || "")}`;
  const hasLlm = analysis?.llm_similarity_enabled;
  const topMatch = hasEstimate && analysis.listings && analysis.listings.length > 0
    ? analysis.listings[0].title
    : "—";
  return `<article class="product-row" data-title="${escapeHtml((product.title || "").toLowerCase())}">
    <a class="product-identity" href="${analysisUrl}">${imageNode}<span>
      <strong title="${escapeHtml(product.title || "")}">${escapeHtml(product.title || "")}</strong>
      <small>موجودی ${fa(product.stock || 0)} · تحلیل بازار</small>
    </span></a>
    <div class="price-block"><small>قیمت فعلی</small><strong>${toman(product.current_price || 0)} تومان</strong></div>
    <div class="range-block"><small>بازه پیشنهادی</small>${range}</div>
    <div class="llm-info"><small>${hasLlm ? "🤖 LLM فعال" : "⚙️ توکن"} · برتر: ${escapeHtml(topMatch)}</small></div>
    <div class="product-actions">
      <a class="analysis-link" href="${analysisUrl}">تحلیل بازار</a>
      <button class="update-price-btn" onclick="recordButtonClick('store_update_price', ${product.product_id})">به‌روزرسانی در باسلام</button>
    </div>
  </article>`;
}

function renderProducts() {
  const query = document.querySelector("#product-filter").value.trim().toLowerCase();
  const products = state.products.filter(product =>
    (product.title || "").toLowerCase().includes(query)
  );
  document.querySelector("#products-list").innerHTML = products.length
    ? products.map(productRow).join("")
    : `<div class="empty-state">${state.products.length ? "محصولی با این نام یافت نشد." : "هنوز محصولی دریافت نشده است."}</div>`;
}

function showLoading(show) {
  document.querySelector("#sync-state").hidden = !show;
  const btn = document.querySelector("#analyze-store");
  if (btn) btn.disabled = show;
  state.status = show ? "loading" : "idle";
}

async function loadStore() {
  const params = new URLSearchParams(window.location.search);
  state.storeId = params.get("id") || window.location.pathname.split("/").pop();
  document.querySelector("#store-title").textContent = `غرفه: ${state.storeId}`;
  await analyzeStore();
}

async function analyzeStore() {
  const limit = parseInt(document.querySelector("#product-limit").value, 10) || 50;
  const useLlm = document.querySelector("#use-llm").checked;
  showLoading(true);
  try {
    const response = await fetch("/api/store/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        store_id: state.storeId,
        product_limit: limit,
        use_llm: useLlm,
      }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.error || body.detail || "خطا در تحلیل غرفه");

    if (body.store && body.store.title) {
      document.querySelector("#store-title").textContent = body.store.title;
    }
    document.querySelector("#store-caption").textContent =
      `${fa(body.total || 0)} محصول بررسی شد • ${fa(body.results.length)} تحلیل موفق`;

    state.products = body.results;
    renderProducts();
  } catch (error) {
    document.querySelector("#store-caption").textContent = "خطا در بارگذاری";
    toast(error.message || String(error));
  } finally {
    showLoading(false);
  }
}

document.querySelector("#product-filter")?.addEventListener("input", renderProducts);
document.querySelector("#analyze-store")?.addEventListener("click", analyzeStore);
document.querySelector("#use-llm")?.addEventListener("change", () => {
  document.querySelector("#use-llm").checked = false;
  toast("قابلیت LLM در حالت آزمایشی است؛ ابتدا کلید API وارد کنید.", { title: "در دسترس نیست" });
});

loadStore().catch(error => toast(error.message || String(error)));
