const views = {
  search: document.querySelector("#search-view"),
  loading: document.querySelector("#loading-view"),
  result: document.querySelector("#result-view"),
  error: document.querySelector("#error-view"),
};
const sourceNames = {
  torob: "ترب",
  digikala: "دیجی‌کالا",
  basalam: "باسلام",
  trendyol: "ترندیول ترکیه",
  noon_uae: "نون امارات",
};
let currentAnalysis = null;
let currentSliderBands = null;
let swipeReviewItems = [];
const reviewedListingUrls = new Set();
let comparableUpdatePending = false;
let userRemovedComparableCount = 0;
let includeForeignPricing = false;
const pageParams = new URLSearchParams(window.location.search);
let merchantContext = {
  active: pageParams.get("from") === "merchant",
  currentPrice: null,
  productId: Number(pageParams.get("product_id")) || null,
};
const toman = value => new Intl.NumberFormat("fa-IR").format(Math.round(value || 0));
const fa = value => new Intl.NumberFormat("fa-IR").format(value || 0);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
}

function normalizeUrl(value) {
  try {
    const normalized = String(value || "").startsWith("//") ? `https:${value}` : value;
    const url = new URL(normalized);
    if (url.protocol === "http:") url.protocol = "https:";
    return url.protocol === "https:" ? url.href : "";
  } catch {
    return "";
  }
}

function safeUrl(value) {
  return escapeHtml(normalizeUrl(value));
}

function cleanMarketplaceInput(value) {
  try {
    const url = new URL(String(value || "").trim());
    const marketplaceHosts = new Set([
      "basalam.com", "www.basalam.com", "torob.com", "www.torob.com",
      "digikala.com", "www.digikala.com", "trendyol.com", "www.trendyol.com",
      "noon.com", "www.noon.com",
    ]);
    if (!marketplaceHosts.has(url.hostname.toLowerCase())) return value;
    url.search = "";
    url.hash = "";
    return url.href;
  } catch {
    return value;
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
  const fairPosition = percentageFor(fair, low, high);
  const optimalHalfWidth = Math.max(1.25, Math.min(3, ((patient - quick) / Math.max(high - low, 1)) * 6));
  return {
    quickStop: Math.max(0, fairPosition - optimalHalfWidth),
    patientStart: Math.min(100, fairPosition + optimalHalfWidth),
    fairPosition,
    rangeLowPosition: percentageFor(quick, low, high),
    rangeHighPosition: percentageFor(patient, low, high),
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
    { name: "quick", position: currentSliderBands.rangeLowPosition },
    { name: "patient", position: currentSliderBands.rangeHighPosition },
  ]);
  priceSlider.style.setProperty("--quick-position", `${currentSliderBands.rangeLowPosition}%`);
  priceSlider.style.setProperty("--patient-position", `${currentSliderBands.rangeHighPosition}%`);
  priceSlider.style.setProperty("--quick-offset", `${rows.quick * 16}px`);
  priceSlider.style.setProperty("--patient-offset", `${rows.patient * 16}px`);
  priceSlider.style.setProperty("--marker-rows", rows.count);
}

async function fetchSampleProducts() {
  const container = document.querySelector("#sample-products");
  const list = document.querySelector("#sample-product-list");
  if (!container || !list) return;
  try {
    const response = await fetch("/api/sample-products");
    if (!response.ok) return;
    const body = await response.json();
    const products = (body.products || []).filter(product => product.url).slice(0, 5);
    if (!products.length) return;
    list.replaceChildren();
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
      const title = product.title || "محصول نمونه باسلام";
      card.innerHTML = `${imageNode}<strong title="${escapeHtml(title)}">${escapeHtml(title)}</strong>`;
      card.addEventListener("click", () => {
        document.querySelector("#product-name").value = product.url;
        analyze(product.url);
      });
      list.appendChild(card);
    });
    container.hidden = false;
  } catch {
    // best-effort: fall back to no sample cards
  }
}

async function analyze(productName) {
  productName = cleanMarketplaceInput(productName);
  reviewedListingUrls.clear();
  userRemovedComparableCount = 0;
  includeForeignPricing = false;
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
    const response = await fetch("/api/market/analyze", {
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
  const analysis = includeForeignPricing
    ? (data.analysis_variants?.with_foreign || data.analysis)
    : (data.analysis_variants?.internal || data.analysis);
  data.analysis = analysis;
  currentAnalysis = data;
  document.querySelector("#result-title").textContent = data.query;

  const queryDisplay = document.querySelector("#search-query-display");
  if (data.resolved_from_url && data.query) {
    const localizedQueries = [...new Set(Object.values(data.search_queries || {}).filter(Boolean))];
    queryDisplay.textContent = localizedQueries.length
      ? `جست‌وجوی هوشمند: ${localizedQueries.join(" · ")}`
      : `جست‌وجو: ${data.query}`;
    queryDisplay.hidden = false;
  } else {
    queryDisplay.hidden = true;
  }

  const enteredPrice = Number(
    data.merchant_product?.current_price
      || data.source_product?.price
      || analysis.recommended,
  );
  document.querySelector("#selected-price").textContent = toman(enteredPrice);

  const sourceProduct = data.source_product;
  const setPriceBtn = document.querySelector("#set-price-basalam");
  if (sourceProduct && sourceProduct.product_id) {
    setPriceBtn.hidden = false;
    setPriceBtn.onclick = () => {
      recordButtonClick("set_price_basalam", sourceProduct.product_id);
      window.open(
        `https://vendor.basalam.com/edit-product/${sourceProduct.product_id}`,
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
        ? `https://vendor.basalam.com/edit-product/${sourceProduct.product_id}`
        : "https://vendor.basalam.com/products";
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
  document.querySelector("#include-foreign-prices").checked = includeForeignPricing;

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
  document.querySelector("#selected-price-label").textContent = "قیمت محصول وارد شده";
  const merchantPriceNote = document.querySelector("#merchant-price-note");
  merchantPriceNote.hidden = !(merchantContext.active && merchantContext.currentPrice);
  if (merchantContext.active && merchantContext.currentPrice) {
    document.querySelector("#merchant-current-price").textContent =
      toman(merchantContext.currentPrice);
  }
  document.querySelector("#merchant-back").hidden = !merchantContext.active;
  updateSelectedPrice();

  const internalAnalysis = data.analysis_variants?.internal || analysis;
  const counts = internalAnalysis.source_counts;
  const parts = Object.entries(counts)
    .filter(([source, count]) => count > 0 && !["trendyol", "noon_uae"].includes(source))
    .map(([source, count]) => `${sourceNames[source]} ${fa(count)}`);
  const datasetNote = data.dataset_count
    ? ` · دیتاست باسلام ${fa(data.dataset_count)} مورد افزوده`
    : "";
  document.querySelector("#source-summary").textContent =
    `${parts.join(" · ")}${datasetNote} · ${fa(Number(internalAnalysis.excluded_count) + userRemovedComparableCount)} قیمت پرت یا نامرتبط حذف شد`;

  const groups = data.listing_groups || {
    internal: analysis.listings.filter(item => !["trendyol", "noon_uae"].includes(item.source)),
    foreign: analysis.listings.filter(item => ["trendyol", "noon_uae"].includes(item.source)),
  };
  renderListingGroup("internal-listings", groups.internal || []);
  renderListingGroup("foreign-listings", groups.foreign || []);
  document.querySelector("#foreign-results").hidden = !(groups.foreign || []).length;
  document.querySelectorAll(".toggle-listings").forEach(button => {
    button.textContent = "نمایش همه";
  });

  setupFeedbackButtons();
  setupSwipeReview();
  setupUseRecommendedButton();
  setupUpdateBasalamButton();
  setupSliderButtons();
}

function nativePriceText(item) {
  const value = Number(item.native_price);
  if (!value || !item.native_currency) return "";
  const formatted = new Intl.NumberFormat("fa-IR", { maximumFractionDigits: 2 }).format(value);
  return `<small class="native-price">${formatted} ${escapeHtml(item.native_currency)}</small>`;
}

function listingCardMarkup(item) {
    const href = safeUrl(item.url);
    const image = safeUrl(item.image_url);
    const imageNode = image
      ? `<img class="listing-image" src="${image}" alt="" loading="lazy" decoding="async" referrerpolicy="no-referrer" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><span class="image-placeholder" hidden>◇</span>`
      : `<span class="image-placeholder">◇</span>`;
    const similarityPct = Math.round(Number(item.llm_similarity ?? item.similarity) * 100);
    const simClass = similarityPct >= 80 ? "high" : similarityPct >= 50 ? "mid" : "low";
    const simLabel = "شباهت";
    const listingKey = encodeURIComponent(href);
    return `<article class="listing-card" data-listing="${listingKey}" data-url="${href}">
      ${imageNode}
      <span class="listing-info" style="flex: 1;">
        <a class="listing-link" href="${href}" target="_blank" rel="noopener noreferrer" title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</a>
        <b>${toman(item.price)} تومان</b>
        ${nativePriceText(item)}
        <small class="sim-badge ${simClass}">${fa(similarityPct)}٪ ${simLabel}</small>
        <small>${escapeHtml(sourceNames[item.source] || item.source)}</small>
      </span>
      <button class="remove-listing-button" type="button" data-remove-url="${href}" aria-label="حذف قیمت ${escapeHtml(item.title)}" title="حذف این قیمت">
        <span aria-hidden="true">×</span>
      </button>
    </article>`;
}

function renderListingGroup(elementId, items) {
  const grid = document.querySelector(`#${elementId}`);
  grid.classList.remove("expanded");
  grid.innerHTML = items.map(listingCardMarkup).join("");
}

function setupFeedbackButtons() {
  const feedback = document.querySelector(".recommendation-feedback");
  const likeBtn = document.querySelector("#like-recommendation");
  const dislikeBtn = document.querySelector("#dislike-recommendation");
  const status = document.querySelector("#recommendation-feedback-status");
  if (feedback) feedback.hidden = false;
  [likeBtn, dislikeBtn].forEach(button => {
    button?.classList.remove("selected");
    button?.setAttribute("aria-pressed", "false");
    if (button) button.disabled = false;
  });
  if (status) status.textContent = "";

  const submit = async (button, rating) => {
    [likeBtn, dislikeBtn].forEach(item => { if (item) item.disabled = true; });
    const saved = await sendFeedback("recommendation", rating);
    [likeBtn, dislikeBtn].forEach(item => {
      if (!item) return;
      item.disabled = false;
      const selected = item === button;
      item.classList.toggle("selected", selected);
      item.setAttribute("aria-pressed", String(selected));
    });
    if (saved) {
      if (feedback) feedback.hidden = true;
      return;
    }
    if (status) status.textContent = "ثبت بازخورد انجام نشد؛ دوباره تلاش کن.";
  };
  if (likeBtn) likeBtn.onclick = () => submit(likeBtn, 1);
  if (dislikeBtn) dislikeBtn.onclick = () => submit(dislikeBtn, -1);
}

function setupUpdateBasalamButton() {
  const btn = document.querySelector("#update-basalam-price");
  if (!btn) return;
  const sourceProduct = currentAnalysis?.source_product;
  if (sourceProduct && sourceProduct.product_id) {
    btn.hidden = false;
    btn.innerHTML = '<span>💰</span>اعمال این قیمت در غرفه';
    btn.onclick = () => {
      recordButtonClick("use_price_in_store", sourceProduct.product_id);
      window.open(`https://vendor.basalam.com/edit-product/${sourceProduct.product_id}`, "_blank", "noopener");
    };
  }
}

function setupUseRecommendedButton() {
  const btn = document.querySelector("#use-recommended");
  if (!btn) return;
  btn.onclick = () => {
    recordButtonClick("use_recommended_price");
    window.location.assign("https://qeimatyar.ir/merchant");
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
          `https://vendor.basalam.com/edit-product/${sourceProduct.product_id}`,
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
    const response = await fetch("/api/feedback", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        feedback_type: feedbackType,
        target_url: window.location.href,
        rating: rating,
        product_name: currentAnalysis?.query || document.querySelector("#result-title")?.textContent || "",
      }),
    });
    return response.ok;
  } catch {
    return false;
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

function sendSimilarityFeedback(listing, rating) {
  return fetch("/api/feedback", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      feedback_type: "similarity",
      target_url: listing.url,
      rating: rating,
      product_name: currentAnalysis?.query || "",
    }),
  }).catch(() => null);
}

async function removeComparable(url) {
  if (comparableUpdatePending) return false;
  const groups = currentAnalysis?.listing_groups || {};
  const listings = [...(groups.internal || []), ...(groups.foreign || [])];
  const normalizedTarget = normalizeUrl(url);
  const listing = listings.find(item => normalizeUrl(item.url) === normalizedTarget);
  if (!listing) return false;
  const internalListings = groups.internal || [];
  const isInternal = !["trendyol", "noon_uae"].includes(listing.source);
  if (listings.length <= 3 || (isInternal && internalListings.length <= 3)) {
    const progress = document.querySelector("#swipe-progress");
    if (progress) progress.textContent = "برای تحلیل حداقل ۳ قیمت از بازار ایران لازم است";
    return false;
  }

  comparableUpdatePending = true;
  document.querySelectorAll(".remove-listing-button").forEach(button => { button.disabled = true; });
  sendSimilarityFeedback(listing, -1);
  try {
    const remaining = listings.filter(item => item !== listing);
    const response = await fetch("/api/market/recalculate", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ listings: remaining }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body.detail || "محاسبه دوباره انجام نشد.");
    userRemovedComparableCount += 1;
    currentAnalysis.analysis = body.analysis;
    currentAnalysis.analysis_variants = body.analysis_variants;
    currentAnalysis.listing_groups = body.listing_groups;
    renderResult(currentAnalysis);
    const progress = document.querySelector("#swipe-progress");
    if (progress) progress.textContent = "قیمت حذف و پیشنهاد به‌روز شد";
    return true;
  } catch (error) {
    const progress = document.querySelector("#swipe-progress");
    if (progress) progress.textContent = error.message;
    document.querySelectorAll(".remove-listing-button").forEach(button => { button.disabled = false; });
    return false;
  } finally {
    comparableUpdatePending = false;
  }
}

function swipeCardMarkup(item, position) {
  const image = safeUrl(item.image_url);
  const imageNode = image
    ? `<img src="${image}" alt="" draggable="false" decoding="async" referrerpolicy="no-referrer" onerror="this.hidden=true;this.nextElementSibling.hidden=false"><span class="swipe-image-placeholder" hidden>◇</span>`
    : `<span class="swipe-image-placeholder">◇</span>`;
  return `<article class="swipe-card${position ? " behind" : ""}" data-swipe-url="${safeUrl(item.url)}">
    <span class="swipe-verdict reject">نامرتبط</span>
    <span class="swipe-verdict accept">مشابه است</span>
    ${imageNode}
    <div><strong>${escapeHtml(item.title)}</strong><b>${toman(item.price)} تومان</b><small>${escapeHtml(sourceNames[item.source] || item.source)}</small></div>
  </article>`;
}

function setupSwipeReview() {
  const panel = document.querySelector("#swipe-review");
  const deck = document.querySelector("#swipe-deck");
  const complete = document.querySelector("#swipe-complete");
  const actions = panel.querySelector(".swipe-actions");
  const listings = currentAnalysis?.analysis?.listings || [];
  swipeReviewItems = listings.filter(item => !reviewedListingUrls.has(item.url)).slice(0, 5);
  panel.hidden = listings.length === 0;
  if (!listings.length) return;
  if (!swipeReviewItems.length) {
    deck.innerHTML = "";
    complete.hidden = false;
    actions.hidden = true;
    return;
  }
  complete.hidden = true;
  actions.hidden = false;

  deck.innerHTML = swipeReviewItems.slice(0, 2).reverse().map((item, reverseIndex, shown) =>
    swipeCardMarkup(item, reverseIndex < shown.length - 1)
  ).join("");
  const progress = document.querySelector("#swipe-progress");
  if (progress) progress.textContent = `${fa(reviewedListingUrls.size)} مورد بررسی شده`;
  const activeCard = deck.querySelector(".swipe-card:not(.behind)");
  if (!activeCard) return;

  let startX = 0;
  let deltaX = 0;
  let dragging = false;
  const move = event => {
    if (!dragging) return;
    deltaX = event.clientX - startX;
    const rotation = Math.max(-12, Math.min(12, deltaX / 18));
    activeCard.style.transform = `translateX(${deltaX}px) rotate(${rotation}deg)`;
    activeCard.style.setProperty("--accept-opacity", Math.max(0, deltaX / 90));
    activeCard.style.setProperty("--reject-opacity", Math.max(0, -deltaX / 90));
  };
  const finish = event => {
    if (!dragging) return;
    dragging = false;
    activeCard.releasePointerCapture?.(event.pointerId);
    activeCard.classList.remove("dragging");
    if (Math.abs(deltaX) >= 85) {
      completeSwipe(deltaX > 0 ? 1 : -1, activeCard);
    } else {
      activeCard.style.transform = "";
      activeCard.style.setProperty("--accept-opacity", 0);
      activeCard.style.setProperty("--reject-opacity", 0);
    }
  };
  activeCard.addEventListener("pointerdown", event => {
    dragging = true;
    deltaX = 0;
    startX = event.clientX;
    activeCard.setPointerCapture?.(event.pointerId);
    activeCard.classList.add("dragging");
  });
  activeCard.addEventListener("pointermove", move);
  activeCard.addEventListener("pointerup", finish);
  activeCard.addEventListener("pointercancel", finish);
}

async function completeSwipe(rating, card = document.querySelector(".swipe-card:not(.behind)")) {
  const listing = swipeReviewItems[0];
  if (!listing || !card || comparableUpdatePending) return;
  card.classList.add(rating > 0 ? "fly-right" : "fly-left");
  reviewedListingUrls.add(listing.url);
  if (rating > 0) {
    await sendSimilarityFeedback(listing, 1);
    setTimeout(setupSwipeReview, 220);
  } else {
    const removed = await removeComparable(listing.url);
    if (!removed) {
      reviewedListingUrls.delete(listing.url);
      card.classList.remove("fly-left");
    }
  }
}

function computeElasticity(value, recommended, low, high) {
  if (!recommended || recommended <= 0) {
    return { demandChangePct: 0, revenueChangePct: 0, distancePct: 0 };
  }
  const distancePct = ((value - recommended) / recommended) * 100;
  const absDistance = Math.abs(distancePct);
  const sideSpan = value >= recommended
    ? Math.max(high - recommended, recommended * 0.05)
    : Math.max(recommended - low, recommended * 0.05);
  const boundDistance = Math.abs(value - recommended) / sideSpan;
  let elasticity = 1.0;
  if (boundDistance < 0.15) {
    elasticity = 0.7;
  } else if (boundDistance < 0.4) {
    elasticity = 1.1;
  } else if (boundDistance < 0.75) {
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

function positionSaleSignal(position) {
  const slider = document.querySelector("#price-slider");
  const signal = document.querySelector("#sale-signal");
  const trackWidth = slider.getBoundingClientRect().width;
  const signalWidth = signal.getBoundingClientRect().width;
  if (!trackWidth || !signalWidth) return;

  const target = (position / 100) * trackWidth;
  const halfSignal = signalWidth / 2;
  const center = signalWidth >= trackWidth
    ? trackWidth / 2
    : Math.min(trackWidth - halfSignal, Math.max(halfSignal, target));
  const pointer = Math.min(signalWidth - 12, Math.max(12, target - center + halfSignal));
  signal.style.setProperty("--signal-left", `${center}px`);
  signal.style.setProperty("--signal-pointer", `${pointer}px`);
}

function updateSelectedPrice() {
  if (!currentAnalysis) return;
  const slider = document.querySelector("#price-slider");
  const value = Number(slider.value);
  const low = Number(slider.min);
  const high = Number(slider.max);
  const position = percentageFor(value, low, high);
  const signal = document.querySelector("#sale-signal");
  const recommended = Number(currentAnalysis.analysis?.recommended || 0);
  const recommendedRange = currentAnalysis.analysis?.range || { low, high };
  const elasticity = computeElasticity(
    value,
    recommended,
    Number(recommendedRange.low),
    Number(recommendedRange.high),
  );
  const demandChange = elasticity.demandChangePct;
  const revenueChange = elasticity.revenueChangePct;
  const distancePct = elasticity.distancePct;
  const recommendedPriceEl = document.querySelector("#recommended-price");
  recommendedPriceEl.textContent = toman(value);

  signal.classList.remove("quick", "fair", "patient");
  const optimalTolerance = Math.max(Number(slider.step) / 2, recommended * 0.001);
  if (value < recommended - optimalTolerance) {
    signal.classList.add("quick");
    document.querySelector("#signal-title").textContent = "فروش سریع‌تر";
    document.querySelector("#signal-copy").textContent = "قیمت شما در بخش رقابتی بازار است و احتمال فروش سریع‌تر می‌شود.";
    slider.style.setProperty("--thumb", "var(--blue)");
    recommendedPriceEl.style.color = "var(--blue)";
  } else if (value <= recommended + optimalTolerance) {
    signal.classList.add("fair");
    document.querySelector("#signal-title").textContent = "قیمت بهینه";
    document.querySelector("#signal-copy").textContent = "در نقطه پیشنهادی بازار هستید.";
    slider.style.setProperty("--thumb", "var(--teal)");
    recommendedPriceEl.style.color = Math.abs(value - recommended) < TOLERANCE ? "var(--green)" : "var(--ink)";
  } else {
    signal.classList.add("patient");
    document.querySelector("#signal-title").textContent = "فروش صبورانه";
    document.querySelector("#signal-copy").textContent = "حاشیه بیشتری دارید، اما ممکن است برای فروش زمان بیشتری لازم باشد.";
    slider.style.setProperty("--thumb", "var(--red)");
    recommendedPriceEl.style.color = "var(--red)";
  }
  positionSaleSignal(position);

  const priceRisk = document.querySelector("#price-risk");
  const riskTitle = document.querySelector("#risk-title");
  const riskCopy = document.querySelector("#risk-copy");
  priceRisk.classList.remove("neutral", "good", "warning", "danger");
  if (Math.abs(value - recommended) < TOLERANCE) {
    priceRisk.classList.add("good");
    riskTitle.textContent = "قیمت پیشنهادی بهینه";
    riskCopy.textContent = "شما دقیقاً روی قیمت پیشنهادی دقیقه هستید. در این نقطه، تعادل مناسبی بین تقاضا و درآمد حفظ می‌شود.";
    recommendedPriceEl.style.color = "var(--green)";
  } else if (value > recommended) {
    const severity = Math.abs(distancePct) > 15 ? "danger" : "warning";
    priceRisk.classList.add(severity);
    riskTitle.textContent = "قیمت بالاتر از پیشنهادی";
    const demandLoss = Math.abs(demandChange);
    const demandLabel = demandLoss > 0 && demandLoss < 1 ? "کمتر از ۱" : fa(Math.round(demandLoss));
    riskCopy.textContent = `با این قیمت، برآورد می‌شود حدود ${demandLabel}٪ از تقاضا را از دست بدهید.`;
    recommendedPriceEl.style.color = severity === "danger" ? "var(--red)" : "var(--orange)";
  } else {
    const severity = Math.abs(distancePct) > 15 ? "danger" : "warning";
    priceRisk.classList.add(severity);
    riskTitle.textContent = "قیمت پایین‌تر از پیشنهادی";
    riskCopy.textContent = `این قیمت ${fa(Math.round(Math.abs(distancePct)))}٪ پایین‌تر از پیشنهاد است و ممکن است درآمد را حدود ${fa(Math.round(Math.abs(revenueChange)))}٪ کاهش دهد.`;
    recommendedPriceEl.style.color = severity === "danger" ? "var(--red)" : "var(--orange)";
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

document.querySelectorAll(".toggle-listings").forEach(button => button.addEventListener("click", event => {
  const grid = document.querySelector(`#${event.currentTarget.dataset.listingTarget}`);
  grid.classList.toggle("expanded");
  event.currentTarget.textContent = grid.classList.contains("expanded") ? "نمایش کمتر" : "نمایش همه";
}));
document.querySelector(".matches").addEventListener("click", event => {
  const button = event.target.closest(".remove-listing-button");
  if (button) {
    event.preventDefault();
    event.stopPropagation();
    removeComparable(button.dataset.removeUrl);
  }
});
document.querySelector("#include-foreign-prices").addEventListener("change", event => {
  includeForeignPricing = event.currentTarget.checked;
  if (currentAnalysis) renderResult(currentAnalysis);
});
document.querySelector("#swipe-reject").addEventListener("click", () => completeSwipe(-1));
document.querySelector("#swipe-accept").addEventListener("click", () => completeSwipe(1));
window.addEventListener("resize", () => {
  applySliderMarkerLayout();
  if (!currentAnalysis) return;
  const slider = document.querySelector("#price-slider");
  positionSaleSignal(percentageFor(Number(slider.value), Number(slider.min), Number(slider.max)));
});

const initialQuery = pageParams.get("q");
if (initialQuery && initialQuery.trim().length >= 2) {
  document.querySelector("#product-name").value = initialQuery.trim();
  analyze(initialQuery.trim());
}
