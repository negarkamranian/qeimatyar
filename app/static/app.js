const views = {
  search: document.querySelector("#search-view"),
  loading: document.querySelector("#loading-view"),
  result: document.querySelector("#result-view"),
  error: document.querySelector("#error-view"),
};
const sourceNames = { torob: "ترب", digikala: "دیجی‌کالا", basalam: "باسلام" };
let currentAnalysis = null;
let currentSliderBands = null;
const pageParams = new URLSearchParams(window.location.search);
let merchantContext = {
  active: pageParams.get("from") === "merchant",
  currentPrice: null,
  productId: Number(pageParams.get("product_id")) || null,
};
const toman = value => new Intl.NumberFormat("fa-IR").format(Math.round(value || 0));
const fa = value => new Intl.NumberFormat("fa-IR").format(value || 0);
let useWebSearch = false;

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function safeUrl(value) {
  try {
    const url = new URL(value);
    return url.protocol === "https:" ? escapeHtml(url.href) : "";
  } catch {
    return "";
  }
}

function showView(name) {
  Object.entries(views).forEach(([key, node]) => { node.hidden = key !== name; });
  window.scrollTo({ top: 0, behavior: "smooth" });
}

function percentageFor(value, low, high) {
  if (high <= low) return 50;
  return Math.min(100, Math.max(0, ((value - low) / (high - low)) * 100));
}

function sliderBands(analysis) {
  const scale = analysis.scale || analysis.range;
  const low = Number(scale.low);
  const high = Number(scale.high);
  const quick = Number(analysis.positions.quick);
  const fair = Number(analysis.positions.fair);
  const patient = Number(analysis.positions.patient);
  const quickStop = percentageFor(quick, low, high);
  const patientStart = percentageFor(patient, low, high);
  return {
    quickStop,
    patientStart: Math.max(quickStop, patientStart),
    fairPosition: percentageFor(fair, low, high),
  };
}

function markerRows(markers) {
  const minGap = window.matchMedia("(max-width: 560px)").matches ? 24 : 15;
  const rowEnds = [];
  const rows = {};

  markers.forEach(marker => {
    let row = rowEnds.findIndex(end => marker.position - end >= minGap);
    if (row === -1) {
      row = rowEnds.length;
      rowEnds.push(marker.position);
    } else {
      rowEnds[row] = marker.position;
    }
    rows[marker.name] = row;
  });

  return { ...rows, count: rowEnds.length };
}

function applySliderMarkerLayout() {
  if (!currentSliderBands) return;
  const priceSlider = document.querySelector(".price-slider");
  const rows = markerRows([
    { name: "quick", position: currentSliderBands.quickStop },
    { name: "patient", position: currentSliderBands.patientStart },
  ]);
  priceSlider.style.setProperty("--quick-position", `${currentSliderBands.quickStop}%`);
  priceSlider.style.setProperty("--patient-position", `${currentSliderBands.patientStart}%`);
  priceSlider.style.setProperty("--quick-offset", `${rows.quick * 16}px`);
  priceSlider.style.setProperty("--patient-offset", `${rows.patient * 16}px`);
  priceSlider.style.setProperty("--marker-rows", rows.count);
}

async function fetchSampleProducts() {
  const container = document.querySelector("#sample-products");
  if (!container) return;
  try {
    const response = await fetch("/api/sample-products");
    if (!response.ok) return;
    const body = await response.json();
    const products = body.products || [];
    container.innerHTML = '<span>یا روی یکی از محصولات نمونه بزنید:</span>';
    products.forEach(product => {
      const image = product.image_url
        ? escapeHtml(product.image_url)
        : "";
      const imageNode = image
        ? `<img class="sample-image" src="${image}" alt="" loading="lazy" referrerpolicy="no-referrer">`
        : `<span class="sample-image-placeholder">◇</span>`;
      const card = document.createElement("button");
      card.type = "button";
      card.className = "sample-product-card";
      card.innerHTML = `${imageNode}<strong title="${escapeHtml(product.title)}">${escapeHtml(product.title)}</strong>`;
      card.addEventListener("click", () => {
        document.querySelector("#product-name").value = product.url;
        analyze(product.url);
      });
      container.appendChild(card);
    });
  } catch {
    // best-effort: fall back to no sample cards
  }
}

async function analyze(productName) {
  const nextUrl = new URL(window.location.href);
  nextUrl.searchParams.set("q", productName);
  window.history.replaceState({}, "", nextUrl);
  showView("loading");
  document.querySelector("#loading-product").textContent =
    /^https?:\/\//i.test(productName) ? "در حال خواندن لینک محصول…" : productName;
  try {
    const requestBody = { product_name: productName };
    if (merchantContext.active && merchantContext.productId) {
      requestBody.exclude_basalam_product_id = merchantContext.productId;
    }
    const endpoint = useWebSearch ? "/api/market/analyze-extended" : "/api/market/analyze";
    const response = await fetch(endpoint, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(requestBody),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body.detail;
      const message = typeof detail === "object" ? detail.message : detail;
      throw new Error(message || "ارتباط با بازارها ناموفق بود.");
    }
    if (merchantContext.active && body.merchant_product) {
      merchantContext.currentPrice = Number(body.merchant_product.current_price) || null;
    }
    renderResult(body);
    showView("result");
  } catch (error) {
    document.querySelector("#error-message").textContent = error.message;
    showView("error");
  }
}

function renderResult(data) {
  const analysis = data.analysis;
  currentAnalysis = data;
  document.querySelector("#result-title").textContent = data.query;

  const queryDisplay = document.querySelector("#search-query-display");
  if (data.resolved_from_url && data.query) {
    queryDisplay.textContent = `جست‌وجو: ${data.query}`;
    queryDisplay.hidden = false;
  } else {
    queryDisplay.hidden = true;
  }

  document.querySelector("#recommended-price").textContent = toman(analysis.recommended);

  const sourceProduct = data.source_product;
  const setPriceBtn = document.querySelector("#set-price-basalam");
  if (sourceProduct && sourceProduct.product_id) {
    setPriceBtn.hidden = false;
    setPriceBtn.onclick = () => {
      recordButtonClick("set_price_basalam", sourceProduct.product_id);
      window.open(
        `https://basalam.com/vendor/products/${sourceProduct.product_id}`,
        "_blank",
        "noopener",
      );
    };
  } else {
    setPriceBtn.hidden = true;
  }

  const updateBtn = document.querySelector("#update-basalam-price");
  if (sourceProduct && sourceProduct.title) {
    updateBtn.hidden = false;
    updateBtn.onclick = () => {
      recordButtonClick("update_price_basalam", sourceProduct.product_id);
      const url = sourceProduct.product_id
        ? `https://basalam.com/vendor/products/${sourceProduct.product_id}`
        : "https://basalam.com/vendor/products";
      window.open(url, "_blank", "noopener");
    };
  } else {
    updateBtn.hidden = true;
  }
  const scale = analysis.scale || analysis.range;
  document.querySelector("#low-label").textContent = toman(analysis.positions.quick);
  document.querySelector("#high-label").textContent = toman(analysis.positions.patient);
  document.querySelector("#market-range").textContent =
    `${toman(analysis.range.low)} تا ${toman(analysis.range.high)} تومان`;
  document.querySelector("#sample-size").textContent = `${fa(analysis.sample_size)} قیمت`;
  document.querySelector("#confidence").textContent = `${fa(analysis.confidence)}٪`;

  const slider = document.querySelector("#price-slider");
  const step = 1000;
  currentSliderBands = sliderBands(analysis);
  slider.style.setProperty("--quick-stop", `${currentSliderBands.quickStop}%`);
  slider.style.setProperty("--patient-start", `${currentSliderBands.patientStart}%`);
  applySliderMarkerLayout();
  slider.min = scale.low;
  slider.max = scale.high;
  slider.step = step;
  slider.value = analysis.recommended;
  document.querySelector("#selected-price-label").textContent =
    merchantContext.active
      ? "قیمت پیشنهادی بازار"
      : "قیمت انتخابی شما";
  const merchantPriceNote = document.querySelector("#merchant-price-note");
  merchantPriceNote.hidden = !(merchantContext.active && merchantContext.currentPrice);
  if (merchantContext.active && merchantContext.currentPrice) {
    document.querySelector("#merchant-current-price").textContent =
      toman(merchantContext.currentPrice);
  }
  document.querySelector("#merchant-back").hidden = !merchantContext.active;
  updateSelectedPrice();

  const counts = analysis.source_counts;
  const parts = Object.entries(counts)
    .filter(([, count]) => count > 0)
    .map(([source, count]) => `${sourceNames[source]} ${fa(count)}`);
  document.querySelector("#source-summary").textContent =
    `${parts.join(" · ")} · ${fa(analysis.excluded_count)} قیمت پرت حذف شد`;

  const listings = document.querySelector("#listings");
  listings.classList.remove("expanded");
  listings.innerHTML = analysis.listings.map((item, index) => {
    const href = safeUrl(item.url);
    const image = safeUrl(item.image_url);
    const imageNode = image
      ? `<img class="listing-image" src="${image}" alt="" loading="lazy" referrerpolicy="no-referrer">`
      : `<span class="image-placeholder">◇</span>`;
    const similarityPct = Math.round(Number(item.llm_similarity ?? item.similarity) * 100);
    const simClass = similarityPct >= 80 ? "high" : similarityPct >= 50 ? "mid" : "low";
    const simLabel = item.llm_similarity != null ? "امتیاز LLM" : "٪ شباهت";
    const listingKey = encodeURIComponent(href);
    return `<a class="listing-card" href="${href}" target="_blank" rel="noopener noreferrer" data-listing="${listingKey}">
      ${imageNode}
      <span class="listing-info" style="flex: 1;">
        <strong title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</strong>
        <b>${toman(item.price)} تومان</b>
        <small class="sim-badge ${simClass}">${fa(similarityPct)}٪ ${simLabel}</small>
        <small>${escapeHtml(sourceNames[item.source] || item.source)}</small>
        
        <div class="listing-state-controls">
          <div class="listing-state-btn" data-state="like" title="پسندیدن">✓</div>
          <div class="listing-state-btn" data-state="unknown" title="نامشخص">؟</div>
          <div class="listing-state-btn" data-state="dislike" title="نپسندیدن">✕</div>
        </div>
      </span>
      <div class="listing-feedback" onclick="handleListingFeedback(event, ${index}, ${similarityPct})">
        <button class="listing-feedback-button dislike" data-action="dislike" title="محصول مشابه نیست">👎</button>
        <button class="listing-feedback-button like" data-action="like" title="محصول مشابه است">👍</button>
      </div>
    </a>`;
  }).join("");
  document.querySelector("#toggle-listings").textContent = "نمایش همه";

  setupFeedbackButtons();
  setupUseRecommendedButton();
  setupUpdateBasalamButton();
  setupSliderButtons();
}

function setupFeedbackButtons() {
  const likeBtn = document.querySelector("#like-recommendation");
  const dislikeBtn = document.querySelector("#dislike-recommendation");
  if (likeBtn) likeBtn.onclick = () => sendFeedback("recommendation", 1);
  if (dislikeBtn) dislikeBtn.onclick = () => sendFeedback("recommendation", -1);
}

function setupUpdateBasalamButton() {
  const btn = document.querySelector("#update-basalam-price");
  if (!btn) return;
  const sourceProduct = currentAnalysis?.source_product;
  if (sourceProduct && sourceProduct.product_id) {
    btn.hidden = false;
    btn.innerHTML = '<span>💰</span>استفاده از این قیمت در غرفه';
    btn.onclick = () => {
      recordButtonClick("use_price_in_store", sourceProduct.product_id);
      window.open(`https://basalam.com/vendor/products/${sourceProduct.product_id}`, "_blank", "noopener");
    };
  }
}

function setupUseRecommendedButton() {
  const btn = document.querySelector("#use-recommended");
  if (!btn) return;
  btn.onclick = () => {
    const slider = document.querySelector("#price-slider");
    slider.value = Number(currentAnalysis?.analysis?.recommended || 0);
    document.querySelector("#selected-price-label").textContent = merchantContext.active
      ? "قیمت انتخابی شما"
      : "قیمت انتخابی شما";
    updateSelectedPrice();
    recordButtonClick("use_recommended_price");
  };
}

function setupSliderButtons() {
  const optimalBtn = document.querySelector("#set-to-optimal");
  const basalamBtn = document.querySelector("#update-in-basalam");
  const sourceProduct = currentAnalysis?.source_product;

  if (optimalBtn) {
    optimalBtn.onclick = () => {
      const slider = document.querySelector("#price-slider");
      const recommended = Number(currentAnalysis?.analysis?.recommended || 0);
      slider.value = recommended;
      updateSelectedPrice();
      recordButtonClick("set_to_optimal_price");
    };
  }

  if (basalamBtn) {
    if (sourceProduct && sourceProduct.product_id) {
      basalamBtn.hidden = false;
      basalamBtn.onclick = () => {
        recordButtonClick("update_price_basalam", sourceProduct.product_id);
        window.open(
          `https://basalam.com/vendor/products/${sourceProduct.product_id}`,
          "_blank",
          "noopener",
        );
      };
    } else {
      basalamBtn.hidden = true;
    }
  }
}

async function sendFeedback(feedbackType, rating) {
  try {
    await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        feedback_type: feedbackType,
        target_url: window.location.href,
        rating: rating,
        product_name: currentAnalysis?.query || document.querySelector("#result-title")?.textContent || "",
      }),
    });
  } catch {
    // feedback tracking is best-effort
  }
}

async function recordButtonClick(buttonName, productId = null) {
  try {
    const sourceProduct = currentAnalysis?.source_product;
    await fetch("/api/metrics/button-click", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        button_name: buttonName,
        product_id: productId,
        store_id: sourceProduct?.store_id || currentAnalysis?.store_id || null,
        product_url: sourceProduct?.product_url || currentAnalysis?.product_url || window.location.href || null,
      }),
    });
  } catch {
    // click tracking is best-effort
  }
}

function handleListingFeedback(event, index, similarityPct) {
  event.preventDefault();
  event.stopPropagation();
  const action = event.target.dataset.action;
  if (!action) return;
  const rating = action === "like" ? 1 : -1;
  const listing = currentAnalysis?.analysis?.listings[index];
  if (!listing) return;
  fetch("/api/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      feedback_type: "similarity",
      target_url: listing.url,
      rating: rating,
      product_name: currentAnalysis?.query || "",
    }),
  }).catch(() => {});

  // visual feedback
  const card = event.target.closest(".listing-card");
  card.style.opacity = "0.5";
  setTimeout(() => { card.style.opacity = ""; }, 1500);
}

function computeElasticity(value, recommended, low, high) {
  if (!recommended || recommended <= 0) {
    return { demandChangePct: 0, revenueChangePct: 0, distancePct: 0 };
  }
  const distancePct = ((value - recommended) / recommended) * 100;
  const absDistance = Math.abs(distancePct);
  let elasticity = 1.0;
  if (absDistance < 3) {
    elasticity = 0.7;
  } else if (absDistance < 8) {
    elasticity = 1.1;
  } else if (absDistance < 15) {
    elasticity = 1.4;
  } else {
    elasticity = 1.8;
  }
  let demandChangePct;
  let revenueChangePct;
  if (value > recommended) {
    demandChangePct = -Math.min(35, absDistance * 0.35 * elasticity);
    revenueChangePct = demandChangePct;
  } else {
    demandChangePct = Math.min(25, absDistance * 0.25 * elasticity);
    revenueChangePct = -Math.min(35, absDistance * 0.28 * elasticity);
  }
  return {
    demandChangePct: Math.round(demandChangePct * 10) / 10,
    revenueChangePct: Math.round(revenueChangePct * 10) / 10,
    distancePct: Math.round(distancePct * 10) / 10,
  };
}

const TOLERANCE = 0.01;

function updateSelectedPrice() {
  if (!currentAnalysis) return;
  const slider = document.querySelector("#price-slider");
  const value = Number(slider.value);
  const low = Number(slider.min);
  const high = Number(slider.max);
  const position = percentageFor(value, low, high);
  const signal = document.querySelector("#sale-signal");
  const recommended = Number(currentAnalysis.recommended || 0);
  const elasticity = computeElasticity(value, recommended, low, high);
  const demandChange = elasticity.demandChangePct;
  const revenueChange = elasticity.revenueChangePct;
  const distancePct = elasticity.distancePct;
  signal.style.setProperty("--signal-position", `${position}%`);
  const selectedPriceEl = document.querySelector("#selected-price");
  selectedPriceEl.textContent = toman(value);

  signal.classList.remove("quick", "fair", "patient");
  if (currentSliderBands && position < currentSliderBands.quickStop) {
    signal.classList.add("quick");
    document.querySelector("#signal-title").textContent = "فروش سریع‌تر";
    document.querySelector("#signal-copy").textContent = "قیمت شما در بخش رقابتی بازار است و احتمال فروش سریع‌تر می‌شود.";
    slider.style.setProperty("--thumb", "var(--blue)");
    selectedPriceEl.style.color = "var(--blue)";
  } else if (currentSliderBands && position <= currentSliderBands.patientStart) {
    signal.classList.add("fair");
    document.querySelector("#signal-title").textContent = "قیمت منصفانه";
    document.querySelector("#signal-copy").textContent = "در مرکز قیمت‌های مشابه بازار قرار دارید.";
    slider.style.setProperty("--thumb", "var(--teal)");
    selectedPriceEl.style.color = Math.abs(value - recommended) < TOLERANCE ? "var(--green)" : "var(--ink)";
  } else {
    signal.classList.add("patient");
    document.querySelector("#signal-title").textContent = "فروش صبورانه";
    document.querySelector("#signal-copy").textContent = "حاشیه بیشتری دارید، اما ممکن است برای فروش زمان بیشتری لازم باشد.";
    slider.style.setProperty("--thumb", "var(--red)");
    selectedPriceEl.style.color = "var(--red)";
  }

  const priceRisk = document.querySelector("#price-risk");
  const riskTitle = document.querySelector("#risk-title");
  const riskCopy = document.querySelector("#risk-copy");
  priceRisk.classList.remove("neutral", "good", "warning", "danger");
  if (Math.abs(value - recommended) < TOLERANCE) {
    priceRisk.classList.add("good");
    riskTitle.textContent = "قیمت پیشنهادی بهینه";
    riskCopy.textContent = "شما دقیقاً روی قیمت پیشنهادی قیمت‌یار هستید. در این نقطه، تعادل مناسبی بین تقاضا و درآمد حفظ می‌شود.";
    selectedPriceEl.style.color = "var(--green)";
  } else if (value > recommended) {
    const severity = Math.abs(distancePct) > 15 ? "danger" : "warning";
    priceRisk.classList.add(severity);
    riskTitle.textContent = "قیمت بالاتر از پیشنهادی";
    riskCopy.textContent = `داری حدود ${Math.abs(demandChange).toFixed(0)}٪ از تقاضا رو از دست میدی.`;
    selectedPriceEl.style.color = severity === "danger" ? "var(--red)" : "var(--orange)";
  } else {
    const severity = Math.abs(distancePct) > 15 ? "danger" : "warning";
    priceRisk.classList.add(severity);
    riskTitle.textContent = "قیمت پایین‌تر از پیشنهادی";
    riskCopy.textContent = `داری حدود ${Math.abs(distancePct).toFixed(0)}٪ قیمت رو پایین میذاری که ${Math.abs(revenueChange).toFixed(0)}٪ از کل درآمد رو کم میکنه.`;
    selectedPriceEl.style.color = severity === "danger" ? "var(--red)" : "var(--orange)";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  fetchSampleProducts();
});

document.querySelector("#price-slider").addEventListener("input", updateSelectedPrice);
document.querySelector("#new-search").addEventListener("click", () => {
  const nextUrl = new URL(window.location.href);
  nextUrl.searchParams.delete("q");
  nextUrl.searchParams.delete("from");
  nextUrl.searchParams.delete("price");
  nextUrl.searchParams.delete("product_id");
  window.history.replaceState({}, "", nextUrl);
  merchantContext = { active: false, currentPrice: null, productId: null };
  document.querySelector("#merchant-back").hidden = true;
  document.querySelector("#merchant-price-note").hidden = true;
  showView("search");
  document.querySelector("#product-name").focus();
});
document.querySelector("#retry-button").addEventListener("click", () => {
  showView("search");
  document.querySelector("#product-name").focus();
});

const searchText = document.querySelector("#product-name");

document.querySelector("#search-form").addEventListener("submit", event => {
  event.preventDefault();
  const productName = searchText.value.trim();
  if (productName.length >= 2) {
    analyze(productName);
  }
});

document.querySelector("#toggle-web-search").addEventListener("click", () => {
  useWebSearch = !useWebSearch;
  const btn = document.querySelector("#toggle-web-search");
  btn.textContent = useWebSearch
    ? "✓ جست‌وجو گسترده وب (نمایش ۱۸ نتیجه از ۳۶)"
    : "جست‌وجو گسترده وب (نمایش ۱۸ نتیجه از ۳۶)";
  btn.style.background = useWebSearch ? "var(--teal)" : "transparent";
  btn.style.color = useWebSearch ? "#fff" : "var(--muted)";
});

document.querySelector("#toggle-listings").addEventListener("click", event => {
  const grid = document.querySelector("#listings");
  grid.classList.toggle("expanded");
  event.currentTarget.textContent = grid.classList.contains("expanded") ? "نمایش کمتر" : "نمایش همه";
});
document.querySelector("#edit-listings").addEventListener("click", event => {
  const grid = document.querySelector("#listings");
  grid.classList.toggle("editing");
  const isEditing = grid.classList.contains("editing");

  event.currentTarget.textContent = isEditing ? "اتمام ویرایش" : "ویرایش";

  if (!isEditing) {
    if (!currentAnalysis || !currentAnalysis.listings) return;
    
    const productName = document.querySelector("#result-title").textContent;
    analyze(productName, currentAnalysis.listings);
  }
});
document.querySelector("#listings").addEventListener("click", event => {
  const grid = event.currentTarget;
  const isEditing = grid.classList.contains("editing");
  const card = event.target.closest(".listing-card");
  
  if (!card) return;
  if (isEditing) { event.preventDefault(); }

  const btn = event.target.closest(".listing-state-btn");
  if (!btn || !isEditing) return;

  const index = card.dataset.index;
  const newState = btn.dataset.state;
  const currentState = card.dataset.currentState;

  // Toggle state logic
  const finalState = currentState === newState ? "default" : newState;

  card.classList.remove("state-like", "state-dislike", "state-unknown");
  card.dataset.currentState = finalState;
  
  if (finalState !== "default") {
    card.classList.add(`state-${finalState}`);
  }

  if (currentAnalysis && currentAnalysis.listings[index]) {
    currentAnalysis.listings[index].userState = finalState;
  }
});
window.addEventListener("resize", applySliderMarkerLayout);

const initialQuery = pageParams.get("q");
if (initialQuery && initialQuery.trim().length >= 2) {
  document.querySelector("#product-name").value = initialQuery.trim();
  analyze(initialQuery.trim());
}
