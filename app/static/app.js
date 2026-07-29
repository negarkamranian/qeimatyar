const views = {
  search: document.querySelector("#search-view"),
  loading: document.querySelector("#loading-view"),
  result: document.querySelector("#result-view"),
  error: document.querySelector("#error-view"),
};
const sourceNames = { torob: "ترب", digikala: "دیجی‌کالا", basalam: "باسلام" };
let currentAnalysis = null;
let currentSliderBands = null;

const toman = value => new Intl.NumberFormat("fa-IR").format(Math.round(value || 0));
const fa = value => new Intl.NumberFormat("fa-IR").format(value || 0);

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

function midpointPercentage(first, second, low, high) {
  return percentageFor((first + second) / 2, low, high);
}

function sliderBands(analysis) {
  const low = Number(analysis.range.low);
  const high = Number(analysis.range.high);
  const quick = Number(analysis.positions.quick);
  const fair = Number(analysis.positions.fair);
  const patient = Number(analysis.positions.patient);
  const quickStop = midpointPercentage(quick, fair, low, high);
  const patientStart = midpointPercentage(fair, patient, low, high);
  return {
    quickStop,
    patientStart: Math.max(quickStop, patientStart),
    fairPosition: percentageFor(fair, low, high),
  };
}

async function analyze(productName) {
  const nextUrl = new URL(window.location.href);
  nextUrl.searchParams.set("q", productName);
  window.history.replaceState({}, "", nextUrl);
  showView("loading");
  document.querySelector("#loading-product").textContent = productName;
  try {
    const response = await fetch("/api/market/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ product_name: productName }),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body.detail;
      const message = typeof detail === "object" ? detail.message : detail;
      throw new Error(message || "ارتباط با بازارها ناموفق بود.");
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
  currentAnalysis = analysis;
  document.querySelector("#result-title").textContent = data.query;
  document.querySelector("#recommended-price").textContent = toman(analysis.recommended);
  document.querySelector("#low-label").textContent = toman(analysis.positions.quick);
  document.querySelector("#fair-label").textContent = toman(analysis.positions.fair);
  document.querySelector("#high-label").textContent = toman(analysis.positions.patient);
  document.querySelector("#market-range").textContent =
    `${toman(analysis.range.low)} تا ${toman(analysis.range.high)} تومان`;
  document.querySelector("#sample-size").textContent = `${fa(analysis.sample_size)} قیمت`;
  document.querySelector("#confidence").textContent = `${fa(analysis.confidence)}٪`;

  const slider = document.querySelector("#price-slider");
  const spread = Math.max(analysis.range.high - analysis.range.low, 2_000);
  const step = spread < 1_000_000 ? 1_000 : 10_000;
  currentSliderBands = sliderBands(analysis);
  slider.style.setProperty("--quick-stop", `${currentSliderBands.quickStop}%`);
  slider.style.setProperty("--patient-start", `${currentSliderBands.patientStart}%`);
  document.querySelector(".price-slider").style.setProperty("--fair-position", `${currentSliderBands.fairPosition}%`);
  slider.min = analysis.range.low;
  slider.max = analysis.range.high;
  slider.step = step;
  slider.value = analysis.recommended;
  updateSelectedPrice();

  const counts = analysis.source_counts;
  const parts = Object.entries(counts)
    .filter(([, count]) => count > 0)
    .map(([source, count]) => `${sourceNames[source]} ${fa(count)}`);
  document.querySelector("#source-summary").textContent =
    `${parts.join(" · ")} · ${fa(analysis.excluded_count)} قیمت پرت حذف شد`;

  const listings = document.querySelector("#listings");
  listings.classList.remove("expanded");
  listings.innerHTML = analysis.listings.map(item => {
    const href = safeUrl(item.url);
    const image = safeUrl(item.image_url);
    const imageNode = image
      ? `<img class="listing-image" src="${image}" alt="" loading="lazy" referrerpolicy="no-referrer">`
      : `<span class="image-placeholder">◇</span>`;
    return `<a class="listing-card" href="${href}" target="_blank" rel="noopener noreferrer">
      ${imageNode}
      <span class="listing-info">
        <strong title="${escapeHtml(item.title)}">${escapeHtml(item.title)}</strong>
        <b>${toman(item.price)} تومان</b>
        <small>${escapeHtml(sourceNames[item.source] || item.source)}</small>
      </span>
    </a>`;
  }).join("");
  document.querySelector("#toggle-listings").textContent = "نمایش همه";
}

function updateSelectedPrice() {
  if (!currentAnalysis) return;
  const slider = document.querySelector("#price-slider");
  const value = Number(slider.value);
  const low = Number(slider.min);
  const high = Number(slider.max);
  const position = percentageFor(value, low, high);
  const signal = document.querySelector("#sale-signal");
  document.querySelector("#selected-price").textContent = toman(value);

  signal.classList.remove("quick", "fair", "patient");
  if (currentSliderBands && position < currentSliderBands.quickStop) {
    signal.classList.add("quick");
    document.querySelector("#signal-title").textContent = "فروش سریع‌تر";
    document.querySelector("#signal-copy").textContent = "قیمت شما در بخش رقابتی بازار است و احتمال فروش سریع‌تر می‌شود.";
    slider.style.setProperty("--thumb", "var(--blue)");
  } else if (currentSliderBands && position <= currentSliderBands.patientStart) {
    signal.classList.add("fair");
    document.querySelector("#signal-title").textContent = "قیمت منصفانه";
    document.querySelector("#signal-copy").textContent = "در مرکز قیمت‌های مشابه بازار قرار دارید.";
    slider.style.setProperty("--thumb", "var(--teal)");
  } else {
    signal.classList.add("patient");
    document.querySelector("#signal-title").textContent = "فروش صبورانه";
    document.querySelector("#signal-copy").textContent = "حاشیه بیشتری دارید، اما ممکن است برای فروش زمان بیشتری لازم باشد.";
    slider.style.setProperty("--thumb", "var(--red)");
  }
}

document.querySelector("#search-form").addEventListener("submit", event => {
  event.preventDefault();
  const input = document.querySelector("#product-name");
  const productName = input.value.trim();
  if (productName.length >= 2) analyze(productName);
});

document.querySelectorAll("[data-example]").forEach(button => {
  button.addEventListener("click", () => {
    document.querySelector("#product-name").value = button.dataset.example;
    analyze(button.dataset.example);
  });
});

document.querySelector("#price-slider").addEventListener("input", updateSelectedPrice);
document.querySelector("#use-recommended").addEventListener("click", () => {
  document.querySelector("#price-slider").value = currentAnalysis.recommended;
  updateSelectedPrice();
});
document.querySelector("#new-search").addEventListener("click", () => {
  const nextUrl = new URL(window.location.href);
  nextUrl.searchParams.delete("q");
  window.history.replaceState({}, "", nextUrl);
  showView("search");
  document.querySelector("#product-name").focus();
});
document.querySelector("#retry-button").addEventListener("click", () => {
  showView("search");
  document.querySelector("#product-name").focus();
});
document.querySelector("#toggle-listings").addEventListener("click", event => {
  const grid = document.querySelector("#listings");
  grid.classList.toggle("expanded");
  event.currentTarget.textContent = grid.classList.contains("expanded") ? "نمایش کمتر" : "نمایش همه";
});

const initialQuery = new URLSearchParams(window.location.search).get("q");
if (initialQuery && initialQuery.trim().length >= 2) {
  document.querySelector("#product-name").value = initialQuery.trim();
  analyze(initialQuery.trim());
}
