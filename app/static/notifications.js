const state = { notifications: [], expanded: new Set() };
const fa = value => new Intl.NumberFormat("fa-IR").format(value || 0);

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"']/g, character => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#039;",
  })[character]);
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

function toast(message) {
  const node = document.querySelector("#toast");
  node.textContent = message;
  node.classList.add("show");
  setTimeout(() => node.classList.remove("show"), 2600);
}

function formatDate(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("fa-IR", {
    dateStyle: "medium", timeStyle: "short",
  }).format(new Date(value));
}

function notificationItem(notification) {
  const expanded = state.expanded.has(notification.id);
  return `<button class="notification-item page-item ${notification.read ? "" : "unread"} ${expanded ? "expanded" : ""}" data-notification-id="${notification.id}" type="button" aria-expanded="${expanded}">
    <span class="notification-dot"></span>
    <span>
      <strong>${escapeHtml(notification.title)}</strong>
      <small>${escapeHtml(expanded ? "برای بستن اعلان دوباره کلیک کنید." : "برای مشاهده متن کامل کلیک کنید.")}</small>
      <time>${escapeHtml(formatDate(notification.created_at))}</time>
      <span class="notification-body">${escapeHtml(notification.body)}</span>
    </span>
  </button>`;
}

function renderNotifications(unreadCount = 0) {
  document.querySelector("#notifications-list").innerHTML = state.notifications.length
    ? state.notifications.map(notificationItem).join("")
    : `<div class="empty-state">اعلانی ندارید.</div>`;
  document.querySelector("#unread-count").textContent = fa(unreadCount);
}

function unreadCount() {
  return state.notifications.filter(notification => !notification.read).length;
}

async function loadNotifications() {
  const data = await api("/api/merchant/notifications?limit=50");
  state.notifications = data.notifications;
  renderNotifications(data.unread_count);
}

async function markNotificationRead(notificationId) {
  await api(`/api/merchant/notifications/${notificationId}/read`, { method: "PATCH" });
  state.notifications = state.notifications.map(notification =>
    notification.id === Number(notificationId)
      ? { ...notification, read: true, read_at: notification.read_at || new Date().toISOString() }
      : notification
  );
  renderNotifications(unreadCount());
}

async function markAllNotificationsRead() {
  await api("/api/merchant/notifications/read-all", { method: "POST" });
  state.notifications = state.notifications.map(notification => ({
    ...notification,
    read: true,
    read_at: notification.read_at || new Date().toISOString(),
  }));
  renderNotifications(0);
}

document.querySelector("#mark-notifications-read").addEventListener("click", async () => {
  try { await markAllNotificationsRead(); }
  catch (error) { toast(error.message); }
});

document.addEventListener("click", event => {
  const notification = event.target.closest("[data-notification-id]");
  if (!notification) return;
  const notificationId = Number(notification.dataset.notificationId);
  if (state.expanded.has(notificationId)) {
    state.expanded.delete(notificationId);
  } else {
    state.expanded.add(notificationId);
  }
  renderNotifications(unreadCount());
  if (notification.classList.contains("unread")) {
    markNotificationRead(notificationId).catch(error => toast(error.message));
  }
});

loadNotifications().catch(error => toast(error.message));
