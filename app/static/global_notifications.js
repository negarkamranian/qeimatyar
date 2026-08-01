(function () {
  const state = window.__globalNotificationState || {
    loaded: false,
    pollTimer: null,
    lastNotifications: [],
    toastTimer: null,
  };
  window.__globalNotificationState = state;

  function escapeHtml(value) {
    return String(value ?? "").replace(/[&<>\"']/g, character => ({
      "&": "&amp;",
      "<": "&lt;",
      ">": "&gt;",
      '"': "&quot;",
      "'": "&#039;",
    })[character]);
  }

  function ensureContainer() {
    let node = document.querySelector("#toast");
    if (!node) {
      node = document.createElement("div");
      node.id = "toast";
      node.setAttribute("role", "status");
      node.setAttribute("aria-live", "polite");
      document.body.appendChild(node);
    }
    return node;
  }

  window.showNotificationToast = function (message, options = {}) {
    const node = ensureContainer();
    const toastNode = document.createElement("div");
    toastNode.className = `toast-card ${options.kind || "notification"}`;
    toastNode.innerHTML = `
      <button class="toast-close" type="button" aria-label="بستن اعلان">×</button>
      <strong>${escapeHtml(options.title || "اعلان جدید")}</strong>
      <span>${escapeHtml(message)}</span>
    `;
    const closeButton = toastNode.querySelector(".toast-close");
    closeButton.addEventListener("click", () => {
      toastNode.classList.remove("show");
      setTimeout(() => toastNode.remove(), 160);
    });
    node.appendChild(toastNode);
    requestAnimationFrame(() => toastNode.classList.add("show"));
    clearTimeout(state.toastTimer);
    state.toastTimer = setTimeout(() => {
      toastNode.classList.remove("show");
      setTimeout(() => toastNode.remove(), 180);
    }, options.timeout || 8000);
    return toastNode;
  };

  function updateBadge(unreadCount) {
    const badge = document.querySelector("#notifications-badge");
    if (!badge) return;
    badge.hidden = unreadCount <= 0;
    badge.textContent = new Intl.NumberFormat("fa-IR").format(unreadCount || 0);
  }

  function showNewNotifications(nextNotifications) {
    if (!state.loaded || !nextNotifications.length) return;
    const previousIds = new Set(state.lastNotifications.map(notification => notification.id));
    const newNotifications = nextNotifications.filter(notification => !previousIds.has(notification.id));
    const newestNotification = newNotifications[0] || null;
    if (!newestNotification) return;
    const body = newestNotification.body || newestNotification.title || "شما یک اعلان جدید دارید.";
    window.showNotificationToast(body, { title: newestNotification.title || "اعلان جدید", kind: "notification" });
  }

  async function loadNotifications() {
    const response = await fetch("/api/merchant/notifications", {
      headers: { "Content-Type": "application/json" },
    });
    if (response.status === 401) return;
    const data = await response.json().catch(() => ({}));
    if (!response.ok) return;
    const notifications = data.notifications || [];
    if (state.loaded) {
      showNewNotifications(notifications);
    } else {
      state.loaded = true;
    }
    state.lastNotifications = notifications;
    updateBadge(data.unread_count || 0);
  }

  function startPolling() {
    if (state.pollTimer) return;
    state.pollTimer = window.setInterval(() => {
      loadNotifications().catch(() => {});
    }, 10000);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", () => {
      startPolling();
      loadNotifications().catch(() => {});
    });
  } else {
    startPolling();
    loadNotifications().catch(() => {});
  }

  document.addEventListener("visibilitychange", () => {
    if (document.visibilityState === "visible") {
      loadNotifications().catch(() => {});
    }
  });
})();
