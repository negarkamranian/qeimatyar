const productId = document.querySelector(".elasticity-main").dataset.productId;
const observations = document.querySelector("#observations");
const result = document.querySelector("#elasticity-result");
const money = value => new Intl.NumberFormat("fa-IR").format(Math.round(value || 0));
const escapeHtml = value => String(value ?? "").replace(/[&<>"']/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;", "'":"&#039;"}[c]));
function addRow(point = {}) {
  const row = document.createElement("div");
  row.className = "observation-row";
  row.innerHTML = `<input name="period" placeholder="مثلاً ۱۴۰۴-۰۱" value="${escapeHtml(point.period || "")}" required><input name="price" type="number" min="1" placeholder="قیمت" value="${point.price || ""}" required><input name="units" type="number" min="0" placeholder="فروش" value="${point.units ?? ""}" required><button type="button" class="remove-row" aria-label="حذف">×</button>`;
  row.querySelector(".remove-row").onclick = () => { if (observations.children.length > 1) row.remove(); };
  observations.append(row);
}
function renderAnalysis(data) {
  const a = data.analysis;
  if (a.status !== "ready") { result.innerHTML = `<div class="result-empty"><strong>داده کافی نیست</strong><span>${escapeHtml(a.message)}</span><small>${a.sample_size} رکورد، ${a.distinct_prices} سطح قیمت</small></div>`; return; }
  const sign = a.elasticity < 0 ? "" : "+";
  const rows = a.scenarios.map(s => `<tr class="${s.price === a.recommended_price ? "best" : ""}"><td>${money(s.price)} تومان</td><td>${money(s.demand_units)} واحد</td><td>${s.demand_change_percent > 0 ? "+" : ""}${s.demand_change_percent}%</td><td>${money(s.revenue)} تومان</td><td>${s.revenue_change_percent > 0 ? "+" : ""}${s.revenue_change_percent}%</td></tr>`).join("");
  result.innerHTML = `<div class="result-head"><div><small>کشش قیمتی برآوردشده</small><strong>${sign}${a.elasticity}</strong><span>به ازای ۱٪ تغییر قیمت، تقاضا حدود ${Math.abs(a.elasticity)}٪ ${a.elasticity < 0 ? "تغییر معکوس" : "تغییر"} می‌کند.</span></div><b>اطمینان ${a.confidence}%</b></div><div class="metric-line"><span>قیمت پیشنهادی برای بیشینه‌کردن درآمد در سناریوها</span><strong>${money(a.recommended_price)} تومان</strong></div><p class="result-message">${escapeHtml(a.interpretation)} ${escapeHtml(a.message)}</p><div class="table-wrap"><table><thead><tr><th>قیمت</th><th>تقاضای پیش‌بینی‌شده</th><th>تغییر تقاضا</th><th>درآمد</th><th>تغییر درآمد</th></tr></thead><tbody>${rows}</tbody></table></div><small class="result-meta">${a.sample_size} رکورد · ${a.distinct_prices} سطح قیمت · R²=${a.r_squared}</small>`;
}
async function load() { const response = await fetch(`/api/merchant/products/${productId}/elasticity`); if (!response.ok) return; const data = await response.json(); renderAnalysis(data); (data.analysis.points || []).filter(p => p.source === "manual").forEach(addRow); if (!observations.children.length) { addRow(); addRow(); addRow(); } }
document.querySelector("#add-observation").onclick = () => addRow();
document.querySelector("#elasticity-form").onsubmit = async event => { event.preventDefault(); const rows = [...observations.children].map(row => Object.fromEntries([...row.querySelectorAll("input")].map(input => [input.name, input.name === "period" ? input.value : Number(input.value)]))); const response = await fetch(`/api/merchant/products/${productId}/elasticity`, { method: "POST", headers: {"Content-Type":"application/json"}, body: JSON.stringify({observations: rows}) }); const data = await response.json(); if (!response.ok) { result.innerHTML = `<div class="result-empty">${escapeHtml(data.detail || "ثبت داده ناموفق بود.")}</div>`; return; } renderAnalysis(data); };
load();
