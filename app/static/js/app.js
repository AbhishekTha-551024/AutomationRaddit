/**
 * Social Media Post Scheduler - Client Application Logic
 * Pure Vanilla JavaScript (Zero External Dependencies)
 */

document.addEventListener("DOMContentLoaded", () => {
  initTheme();
  initToast();
  initComposer();
  initRetries();
  initCopyButtons();
  initAutoRefresh();
});

/* ==========================================================================
   1. Theme Management (Light / Dark with LocalStorage & OS Preference)
   ========================================================================== */
function initTheme() {
  const storedTheme = localStorage.getItem("theme");
  const systemPrefersDark = window.matchMedia("(prefers-color-scheme: dark)").matches;
  const initialTheme = storedTheme || (systemPrefersDark ? "dark" : "light");

  document.documentElement.setAttribute("data-theme", initialTheme);
  updateThemeToggleLabels(initialTheme);

  const toggleButtons = document.querySelectorAll(".theme-toggle-btn");
  toggleButtons.forEach((btn) => {
    btn.addEventListener("click", () => {
      const currentTheme = document.documentElement.getAttribute("data-theme") || "light";
      const newTheme = currentTheme === "dark" ? "light" : "dark";

      document.documentElement.setAttribute("data-theme", newTheme);
      localStorage.setItem("theme", newTheme);
      updateThemeToggleLabels(newTheme);
    });
  });
}

function updateThemeToggleLabels(theme) {
  const labels = document.querySelectorAll(".theme-toggle-label");
  labels.forEach((el) => {
    el.textContent = theme === "dark" ? "Light Mode" : "Dark Mode";
  });
}

/* ==========================================================================
   2. Toast Notification System
   ========================================================================== */
function initToast() {
  if (!document.getElementById("toast-container")) {
    const container = document.createElement("div");
    container.id = "toast-container";
    container.className = "toast-container";
    document.body.appendChild(container);
  }

  // Check URL query for flash toast (e.g., ?toast=Post%20scheduled)
  const urlParams = new URLSearchParams(window.location.search);
  const toastMsg = urlParams.get("toast");
  if (toastMsg) {
    showToast(decodeURIComponent(toastMsg));
    // Clean up query param from URL without reload
    const cleanUrl = window.location.pathname;
    window.history.replaceState({}, document.title, cleanUrl);
  }
}

function showToast(message, type = "info") {
  let container = document.getElementById("toast-container");
  if (!container) {
    container = document.createElement("div");
    container.id = "toast-container";
    container.className = "toast-container";
    document.body.appendChild(container);
  }

  const isError = type === "error" || (typeof message === "string" && message.toLowerCase().startsWith("error:"));
  const toast = document.createElement("div");
  toast.className = `toast ${isError ? "toast-error" : ""}`;
  if (isError) {
    toast.style.borderColor = "var(--status-failed-text, #f85149)";
  }

  const iconSvg = isError
    ? `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#f85149" stroke-width="2"><circle cx="12" cy="12" r="10"></circle><line x1="15" y1="9" x2="9" y2="15"></line><line x1="9" y1="9" x2="15" y2="15"></line></svg>`
    : `<svg width="16" height="16" viewBox="0 0 24 24" fill="none" stroke="#2ea043" stroke-width="2"><path d="M20 6L9 17l-5-5"/></svg>`;

  toast.innerHTML = `
    ${iconSvg}
    <span style="color: ${isError ? '#f85149' : 'inherit'};">${escapeHtml(message)}</span>
  `;

  container.appendChild(toast);

  setTimeout(() => {
    toast.style.opacity = "0";
    toast.style.transform = "translateY(10px)";
    toast.style.transition = "all 0.25s ease-out";
    setTimeout(() => toast.remove(), 250);
  }, 4000);
}

window.showToast = showToast;

/* ==========================================================================
   3. Composer Live Behavior (Character Counters, Presets, Live Preview)
   ========================================================================== */
const PLATFORM_LIMITS = {
  discord: 2000,
  telegram: 4096,
  threads: 500,
  instagram: 2200,
};

function splitThreadPartsJs(title, body) {
  const rawLines = body.split(/\r?\n/);
  const parts = [];
  let currentLines = [];

  for (const line of rawLines) {
    if (line.trim() === "---") {
      if (currentLines.length > 0) {
        const chunk = currentLines.join("\n").trim();
        if (chunk) parts.push(chunk);
        currentLines = [];
      }
    } else {
      currentLines.push(line);
    }
  }
  if (currentLines.length > 0) {
    const chunk = currentLines.join("\n").trim();
    if (chunk) parts.push(chunk);
  }

  if (parts.length === 0 && body.trim()) {
    parts.push(body.trim());
  }

  if (title && title.trim()) {
    if (parts.length > 0) {
      parts[0] = title.trim() + "\n\n" + parts[0];
    } else {
      parts.push(title.trim());
    }
  }

  return parts;
}

function initComposer() {
  const composerForm = document.getElementById("composer-form");
  if (!composerForm) return;

  const titleInput = document.getElementById("post-title");
  const bodyInput = document.getElementById("post-body");
  const mediaInput = document.getElementById("post-media-url");
  const datetimeInput = document.getElementById("scheduled-datetime");
  const clarityLabel = document.getElementById("schedule-clarity-label");
  const chips = document.querySelectorAll(".platform-chip");
  const submitBtn = document.getElementById("submit-post-btn");
  const previewBody = document.getElementById("preview-body-text");
  const previewTitle = document.getElementById("preview-title-text");
  const previewSingle = document.getElementById("preview-single-container");
  const previewThread = document.getElementById("preview-thread-container");
  const previewMediaContainer = document.getElementById("preview-media-container");
  const previewMediaImg = document.getElementById("preview-media-img");

  // Expose preview updater globally for quick selection buttons
  window.composerUpdatePreview = updateMetersAndPreview;

  // Platform Chip Toggles
  chips.forEach((chip) => {
    const checkbox = chip.querySelector("input[type='checkbox']");
    if (!checkbox) return;

    // Listen to native change event
    checkbox.addEventListener("change", () => {
      chip.classList.toggle("selected", checkbox.checked);
      updateMetersAndPreview();
    });

    // Keyboard accessibility: Space or Enter on chip
    chip.setAttribute("tabindex", "0");
    chip.addEventListener("keydown", (e) => {
      if (e.key === " " || e.key === "Enter") {
        e.preventDefault();
        checkbox.checked = !checkbox.checked;
        checkbox.dispatchEvent(new Event("change", { bubbles: true }));
      }
    });

    // Sync initial state
    chip.classList.toggle("selected", checkbox.checked);
  });

  // Live Input Sync
  titleInput.addEventListener("input", updateMetersAndPreview);
  bodyInput.addEventListener("input", updateMetersAndPreview);
  if (mediaInput) {
    mediaInput.addEventListener("input", updateMetersAndPreview);
  }

  function updateMetersAndPreview() {
    const titleVal = titleInput.value.trim();
    const bodyVal = bodyInput.value;
    const combinedLength = (titleVal ? titleVal + "\n\n" : "") + bodyVal;
    const count = combinedLength.length;
    const selectedPlatforms = getSelectedPlatforms();
    const threadParts = splitThreadPartsJs(titleVal, bodyVal);
    const mediaVal = mediaInput ? mediaInput.value.trim() : "";

    // Update Media Image Preview
    if (previewMediaContainer && previewMediaImg) {
      if (mediaVal && (mediaVal.startsWith("http://") || mediaVal.startsWith("https://"))) {
        previewMediaImg.src = mediaVal;
        previewMediaContainer.style.display = "block";
      } else {
        previewMediaContainer.style.display = "none";
      }
    }

    // Update Live Preview Pane
    if (selectedPlatforms.includes("threads") && threadParts.length > 1) {
      // Stacked thread preview mode
      if (previewSingle) previewSingle.style.display = "none";
      if (previewThread) {
        previewThread.style.display = "block";
        previewThread.innerHTML = threadParts
          .map((part, i) => {
            const isOver = part.length > 500;
            return `
              <div class="thread-chain-item" style="${isOver ? 'border-color: var(--status-failed-text);' : ''}">
                <div class="thread-part-badge" style="${isOver ? 'color: var(--status-failed-text);' : ''}">
                  Part ${i + 1} of ${threadParts.length} &bull; ${part.length}/500 chars ${isOver ? '(EXCEEDED)' : ''}
                </div>
                <div>${escapeHtml(part)}</div>
              </div>
            `;
          })
          .join("");
      }
    } else {
      // Single post preview mode
      if (previewThread) previewThread.style.display = "none";
      if (previewSingle) previewSingle.style.display = "block";

      if (previewTitle) {
        if (titleVal) {
          previewTitle.textContent = titleVal;
          previewTitle.style.display = "block";
        } else {
          previewTitle.style.display = "none";
        }
      }
      if (previewBody) {
        previewBody.textContent = bodyVal || "Preview will appear here as you type...";
        previewBody.style.color = bodyVal ? "inherit" : "var(--text-muted)";
      }
    }

    // Check limits for each active platform
    let isAnyOverLimit = false;

    document.querySelectorAll(".char-meter").forEach((meter) => {
      const platform = meter.getAttribute("data-platform");
      if (!selectedPlatforms.includes(platform)) {
        meter.style.display = "none";
        return;
      }

      meter.style.display = "inline";
      meter.classList.remove("warning", "danger");

      if (platform === "threads") {
        if (threadParts.length > 1) {
          // Multiple thread parts meter
          let threadMeterHtml = `THREADS (${threadParts.length} parts): `;
          let hasThreadOverflow = false;
          const partSummaries = threadParts.map((p, idx) => {
            const pLen = p.length;
            if (pLen > 500) {
              hasThreadOverflow = true;
              return `<span style="color: var(--status-failed-text); font-weight: 700;">P${idx + 1}: ${pLen}/500</span>`;
            }
            return `P${idx + 1}: ${pLen}/500`;
          });
          meter.innerHTML = threadMeterHtml + partSummaries.join(" &bull; ");

          if (hasThreadOverflow) {
            meter.classList.add("danger");
            isAnyOverLimit = true;
          }
        } else {
          // Single post meter for Threads
          const max = 500;
          meter.textContent = `THREADS: ${count} / ${max}`;
          if (count > max) {
            meter.classList.add("danger");
            isAnyOverLimit = true;
          } else if (count >= max * 0.8) {
            meter.classList.add("warning");
          }
        }
      } else {
        const max = PLATFORM_LIMITS[platform] || 2000;
        meter.textContent = `${platform.toUpperCase()}: ${count} / ${max}`;

        if (count > max) {
          meter.classList.add("danger");
          isAnyOverLimit = true;
        } else if (count >= max * 0.8) {
          meter.classList.add("warning");
        }
      }
    });

    // Disable submit if over limit, empty body, or no platforms selected
    const isBodyEmpty = bodyVal.trim().length === 0;
    const noPlatform = selectedPlatforms.length === 0;
    submitBtn.disabled = isAnyOverLimit || isBodyEmpty || noPlatform;
  }

  // Quick Scheduling Presets
  document.querySelectorAll(".preset-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      const preset = btn.getAttribute("data-preset");
      const targetDate = calculatePresetTime(preset);
      datetimeInput.value = formatForDateTimeInput(targetDate);
      updateScheduleClarityText(targetDate);
    });
  });

  // When user manual edits datetime input
  datetimeInput.addEventListener("input", () => {
    if (datetimeInput.value) {
      const parsed = new Date(datetimeInput.value);
      updateScheduleClarityText(parsed);
    }
  });

  function updateScheduleClarityText(dateObj) {
    if (!clarityLabel) return;
    const options = {
      weekday: "short",
      day: "numeric",
      month: "short",
      hour: "numeric",
      minute: "2-digit",
      hour12: true,
      timeZone: "Asia/Kolkata",
    };
    const formatted = new Intl.DateTimeFormat("en-IN", options).format(dateObj);
    clarityLabel.textContent = `Will post ${formatted} IST`;
  }

  // Set default schedule time (In 5 minutes)
  const defaultDate = new Date(Date.now() + 5 * 60 * 1000);
  datetimeInput.value = formatForDateTimeInput(defaultDate);
  updateScheduleClarityText(defaultDate);

  // Keyboard Shortcut: Ctrl + Enter / Cmd + Enter to submit
  document.addEventListener("keydown", (e) => {
    if ((e.ctrlKey || e.metaKey) && e.key === "Enter") {
      if (!submitBtn.disabled) {
        composerForm.requestSubmit();
      }
    }
  });

  // Form Submit Handler (AJAX to POST /posts with redirect)
  composerForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    clearFieldErrors();

    const title = titleInput.value.trim() || null;
    const body = bodyInput.value.trim();
    const mediaVal = mediaInput ? mediaInput.value.trim() : "";
    const scheduledRaw = datetimeInput.value;
    const platforms = getSelectedPlatforms();

    // Inline field validation
    let hasError = false;
    if (!body) {
      showFieldError("post-body-error", "Please write your post message content.");
      hasError = true;
    }
    if (platforms.length === 0) {
      showFieldError("platform-error", "Please select at least one platform target.");
      hasError = true;
    }
    if (platforms.includes("instagram") && !mediaVal) {
      showFieldError("post-media-error", "Instagram requires a public image or video URL.");
      hasError = true;
    }
    if (!scheduledRaw) {
      showFieldError("datetime-error", "Please choose a scheduled time.");
      hasError = true;
    }

    if (hasError) return;

    submitBtn.disabled = true;
    submitBtn.textContent = "Scheduling...";

    const payload = {
      title: title,
      body: body,
      media_url: mediaVal || null,
      targets: platforms.map((p) => ({
        platform: p,
        scheduled_at: scheduledRaw,
      })),
    };

    try {
      const resp = await fetch("/posts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      if (!resp.ok) {
        const errorData = await resp.json();
        throw new Error(errorData.detail || "Failed to schedule post");
      }

      const created = await resp.json();
      // Redirect to post detail view with success toast
      window.location.href = `/posts/${created.id}/view?toast=Post%20scheduled%20successfully`;
    } catch (err) {
      showFieldError("form-general-error", err.message);
      submitBtn.disabled = false;
      submitBtn.textContent = "Schedule post";
    }
  });

  updateMetersAndPreview();
}

function getSelectedPlatforms() {
  const selected = [];
  document.querySelectorAll(".platform-chip input[type='checkbox']").forEach((cb) => {
    if (cb.checked) selected.push(cb.value);
  });
  return selected;
}

function selectOnlyPlatform(targetPlatform) {
  const norm = (targetPlatform || "").toLowerCase();
  document.querySelectorAll(".platform-chip input[type='checkbox']").forEach((cb) => {
    cb.checked = (cb.value.toLowerCase() === norm);
    const chip = cb.closest(".platform-chip");
    if (chip) chip.classList.toggle("selected", cb.checked);
  });
  if (typeof window.composerUpdatePreview === "function") {
    window.composerUpdatePreview();
  }
  const editBody = document.getElementById("edit-post-body");
  if (editBody) editBody.dispatchEvent(new Event("input"));
}

function selectAllPlatforms(selectAll = true) {
  document.querySelectorAll(".platform-chip input[type='checkbox']").forEach((cb) => {
    cb.checked = selectAll;
    const chip = cb.closest(".platform-chip");
    if (chip) chip.classList.toggle("selected", cb.checked);
  });
  if (typeof window.composerUpdatePreview === "function") {
    window.composerUpdatePreview();
  }
  const editBody = document.getElementById("edit-post-body");
  if (editBody) editBody.dispatchEvent(new Event("input"));
}

function calculatePresetTime(preset) {
  const now = new Date();
  if (preset === "now") {
    return new Date(now.getTime() + 10 * 1000); // 10s from now
  } else if (preset === "1hour") {
    return new Date(now.getTime() + 60 * 60 * 1000);
  } else if (preset === "tonight") {
    const tonight = new Date();
    tonight.setHours(20, 0, 0, 0); // 8:00 PM
    if (tonight <= now) {
      // If already past 8 PM, set to tomorrow night
      tonight.setDate(tonight.getDate() + 1);
    }
    return tonight;
  } else if (preset === "tomorrow_morning") {
    const tomorrow = new Date();
    tomorrow.setDate(tomorrow.getDate() + 1);
    tomorrow.setHours(9, 0, 0, 0); // 9:00 AM
    return tomorrow;
  }
  return now;
}

function formatForDateTimeInput(date) {
  // Format local time into YYYY-MM-DDTHH:MM for <input type="datetime-local">
  const pad = (n) => String(n).padStart(2, "0");
  const year = date.getFullYear();
  const month = pad(date.getMonth() + 1);
  const day = pad(date.getDate());
  const hours = pad(date.getHours());
  const minutes = pad(date.getMinutes());
  return `${year}-${month}-${day}T${hours}:${minutes}`;
}

function showFieldError(id, msg) {
  const el = document.getElementById(id);
  if (el) {
    el.textContent = msg;
    el.classList.add("visible");
  }
}

function clearFieldErrors() {
  document.querySelectorAll(".field-error-message").forEach((el) => {
    el.textContent = "";
    el.classList.remove("visible");
  });
}

/* ==========================================================================
   4. One-Click Retries for Failed Targets
   ========================================================================== */
function initRetries() {
  document.addEventListener("click", async (e) => {
    const btn = e.target.closest(".btn-retry-target");
    if (!btn) return;

    const targetId = btn.getAttribute("data-target-id");
    btn.disabled = true;
    btn.textContent = "Retrying...";

    try {
      const resp = await fetch(`/targets/${targetId}/retry`, { method: "POST" });
      if (!resp.ok) {
        const errData = await resp.json();
        throw new Error(errData.detail || "Retry request failed");
      }

      showToast(`Target #${targetId} reset to pending`);
      // Update UI row without full reload
      setTimeout(() => window.location.reload(), 600);
    } catch (err) {
      showToast(`Error: ${err.message}`);
      btn.disabled = false;
      btn.textContent = "Retry now";
    }
  });
}

/* ==========================================================================
   5. Copy Post Text to Clipboard
   ========================================================================== */
function initCopyButtons() {
  const copyBtn = document.getElementById("copy-post-text-btn");
  if (!copyBtn) return;

  copyBtn.addEventListener("click", () => {
    const content = copyBtn.getAttribute("data-copy-content") || "";
    navigator.clipboard.writeText(content).then(() => {
      showToast("Post text copied to clipboard");
    });
  });
}

/* ==========================================================================
   6. Auto-Refresh (Every 30 Seconds for Dashboard)
   ========================================================================== */
function initAutoRefresh() {
  const dashboardContainer = document.getElementById("dashboard-root");
  if (!dashboardContainer) return;

  setInterval(async () => {
    try {
      const resp = await fetch(window.location.href, {
        headers: { "X-Requested-With": "XMLHttpRequest" },
      });
      if (resp.ok) {
        const html = await resp.text();
        const parser = new DOMParser();
        const newDoc = parser.parseFromString(html, "text/html");
        const newDashboard = newDoc.getElementById("dashboard-root");
        if (newDashboard) {
          dashboardContainer.innerHTML = newDashboard.innerHTML;
        }
      }
    } catch (e) {
      // Quiet background poller failure; no intrusive error
    }
  }, 30000);
}

function escapeHtml(str) {
  return String(str)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;")
    .replace(/'/g, "&#039;");
}

/* ==========================================================================
   7. Operations & Post Management Handlers
   ========================================================================== */

// Action dropdown menu toggling & keyboard navigation
function toggleActionMenu(event, menuId) {
  event.stopPropagation();
  const menu = document.getElementById(menuId);
  const isShown = menu && menu.classList.contains("show");

  document.querySelectorAll(".action-menu.show").forEach((m) => {
    m.classList.remove("show");
    const trigger = document.querySelector(`[aria-controls="${m.id}"]`);
    if (trigger) trigger.setAttribute("aria-expanded", "false");
  });

  if (menu && !isShown) {
    menu.classList.add("show");
    const btn = event.currentTarget;
    if (btn) btn.setAttribute("aria-expanded", "true");
  }
}

function handleMenuButtonKey(event, menuId) {
  if (event.key === "Enter" || event.key === " " || event.key === "ArrowDown") {
    event.preventDefault();
    toggleActionMenu(event, menuId);
    const menu = document.getElementById(menuId);
    if (menu) {
      const firstItem = menu.querySelector(".action-menu-item, button, a");
      if (firstItem) firstItem.focus();
    }
  } else if (event.key === "Escape") {
    const menu = document.getElementById(menuId);
    if (menu) menu.classList.remove("show");
    event.currentTarget.setAttribute("aria-expanded", "false");
  }
}

document.addEventListener("keydown", (e) => {
  if (e.key === "Escape") {
    document.querySelectorAll(".action-menu.show").forEach((m) => {
      m.classList.remove("show");
      const btn = document.querySelector(`[aria-controls="${m.id}"]`);
      if (btn) btn.focus();
    });
  }
});

document.addEventListener("click", () => {
  document.querySelectorAll(".action-menu.show").forEach((m) => {
    m.classList.remove("show");
    const btn = document.querySelector(`[aria-controls="${m.id}"]`);
    if (btn) btn.setAttribute("aria-expanded", "false");
  });
});

// Delete Management:
// - Deleting pending, failed, draft, or cancelled: NO modal; deletes immediately with 8-second Undo toast.
// - Deleting posted item: Confirmation modal ("Removes it from this dashboard only. It stays live on the platform. You can restore it for 30 days.").
let deleteModalContext = null;

function deleteItem(type, id, status) {
  if (status === "posted") {
    openDeleteModal(type, id, status);
  } else {
    executeImmediateDelete(type, id);
  }
}

function openDeleteModal(type, id, status) {
  deleteModalContext = { type, id, status };
  const modal = document.getElementById("delete-modal");
  const body = document.getElementById("delete-modal-body");
  if (body) {
    body.textContent = "Removes it from this dashboard only. It stays live on the platform. You can restore it for 30 days.";
  }
  if (modal) modal.classList.add("active");
}

function closeDeleteModal() {
  deleteModalContext = null;
  const modal = document.getElementById("delete-modal");
  if (modal) modal.classList.remove("active");
}

async function executeConfirmedDelete() {
  if (!deleteModalContext) return;
  const { type, id } = deleteModalContext;
  closeDeleteModal();
  await executeImmediateDelete(type, id);
}

async function executeImmediateDelete(type, id) {
  const row = document.getElementById(type === "post" ? `row-post-${id}` : `row-target-${id}`) ||
              document.getElementById(`failed-item-${id}`);
  if (row) row.style.opacity = "0.35";

  try {
    const url = type === "post" ? `/posts/${id}` : `/targets/${id}`;
    const resp = await fetch(url, { method: "DELETE" });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Delete failed");
    }

    showUndoToast("Moved to Trash. Undo", async () => {
      const restoreUrl = type === "post" ? `/posts/${id}/restore` : `/targets/${id}/restore`;
      const resResp = await fetch(restoreUrl, { method: "POST" });
      if (resResp.ok) {
        if (row) row.style.opacity = "1";
        showToast("Restored from Trash");
      }
    });
  } catch (err) {
    if (row) row.style.opacity = "1";
    showToast(`Error: ${err.message}`);
  }
}

// 8-Second Undo Toast System
let undoTimer = null;
let undoCallbackFn = null;

function showUndoToast(message, callback) {
  const wrapper = document.getElementById("undo-toast-wrapper");
  const msgEl = document.getElementById("undo-toast-msg");
  const timerEl = document.getElementById("undo-timer-count");
  if (!wrapper || !msgEl || !timerEl) {
    showToast(message);
    return;
  }

  if (undoTimer) clearInterval(undoTimer);
  undoCallbackFn = callback;
  msgEl.textContent = "Moved to Trash.";

  let timeLeft = 8;
  timerEl.textContent = timeLeft;
  wrapper.style.display = "block";

  undoTimer = setInterval(() => {
    timeLeft -= 1;
    timerEl.textContent = timeLeft;
    if (timeLeft <= 0) {
      clearInterval(undoTimer);
      wrapper.style.display = "none";
      undoCallbackFn = null;
      setTimeout(() => window.location.reload(), 200);
    }
  }, 1000);
}

async function triggerUndoAction() {
  if (undoTimer) clearInterval(undoTimer);
  const wrapper = document.getElementById("undo-toast-wrapper");
  if (wrapper) wrapper.style.display = "none";

  if (undoCallbackFn) {
    try {
      await undoCallbackFn();
    } catch (e) {
      showToast(`Undo failed: ${e.message}`);
    }
    undoCallbackFn = null;
  }
}

// Reschedule Modal
let rescheduleTargetId = null;

function openRescheduleModal(targetId) {
  rescheduleTargetId = targetId;
  const modal = document.getElementById("reschedule-modal");
  const dtInput = document.getElementById("reschedule-input-datetime");
  if (dtInput) {
    const future = new Date(Date.now() + 60 * 60 * 1000); // 1h in future
    dtInput.value = formatForDateTimeInput(future);
  }
  if (modal) modal.classList.add("active");
}

function closeRescheduleModal() {
  rescheduleTargetId = null;
  const modal = document.getElementById("reschedule-modal");
  if (modal) modal.classList.remove("active");
}

async function executeConfirmedReschedule() {
  if (!rescheduleTargetId) return;
  const dtInput = document.getElementById("reschedule-input-datetime");
  if (!dtInput || !dtInput.value) {
    alert("Please select a date and time.");
    return;
  }
  const tid = rescheduleTargetId;
  closeRescheduleModal();

  try {
    const resp = await fetch(`/targets/${tid}/schedule`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ scheduled_at: dtInput.value }),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Reschedule failed");
    }
    showToast("Rescheduled successfully");
    setTimeout(() => window.location.reload(), 500);
  } catch (err) {
    showToast(`Error: ${err.message}`);
  }
}

// Individual Target Actions
async function postTargetNow(targetId) {
  try {
    const resp = await fetch(`/targets/${targetId}/post-now`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Post now failed");
    }
    showToast(`Target #${targetId} scheduled for immediate dispatch`);
    setTimeout(() => window.location.reload(), 500);
  } catch (err) {
    showToast(`Error: ${err.message}`);
  }
}

async function cancelTarget(targetId) {
  try {
    const resp = await fetch(`/targets/${targetId}/cancel`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Cancel failed");
    }
    showToast(`Target #${targetId} cancelled`);
    setTimeout(() => window.location.reload(), 500);
  } catch (err) {
    showToast(`Error: ${err.message}`);
  }
}

async function retryTarget(targetId) {
  try {
    const resp = await fetch(`/targets/${targetId}/retry`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Retry failed");
    }
    showToast(`Target #${targetId} reset to pending`);
    setTimeout(() => window.location.reload(), 500);
  } catch (err) {
    showToast(`Error: ${err.message}`);
  }
}

async function restoreTarget(targetId) {
  try {
    const resp = await fetch(`/targets/${targetId}/restore`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Restore failed");
    }
    showToast(`Target #${targetId} restored`);
    setTimeout(() => window.location.reload(), 500);
  } catch (err) {
    showToast(`Error: ${err.message}`);
  }
}

async function permanentDeleteTarget(targetId) {
  if (!confirm("Permanently delete this target? This action cannot be undone.")) return;
  try {
    const resp = await fetch(`/trash/${targetId}/permanent?item_type=target`, { method: "DELETE" });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Permanent delete failed");
    }
    showToast(`Target #${targetId} permanently deleted`);
    const row = document.getElementById(`row-trash-${targetId}`);
    if (row) row.remove();
  } catch (err) {
    showToast(`Error: ${err.message}`);
  }
}

async function duplicatePost(postId) {
  try {
    const resp = await fetch(`/posts/${postId}/duplicate`, { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Duplicate failed");
    }
    const data = await resp.json();
    window.location.href = `/posts/${data.new_post_id}/edit?toast=Draft%20created%20from%20post%20%23${postId}`;
  } catch (err) {
    showToast(`Error: ${err.message}`);
  }
}

async function confirmEmptyTrash() {
  if (!confirm("Are you sure you want to permanently delete all items in Trash? This cannot be undone.")) return;
  try {
    const resp = await fetch("/trash/empty", { method: "POST" });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Failed to empty trash");
    }
    const data = await resp.json();
    showToast(`Trash emptied: ${data.purged_count} item(s) permanently removed`);
    setTimeout(() => window.location.reload(), 500);
  } catch (err) {
    showToast(`Error: ${err.message}`);
  }
}

// Composer Save as Draft Helper
async function submitComposerPost(isDraft = false) {
  const form = document.getElementById("composer-form");
  if (!form) return;

  clearFieldErrors();
  const title = document.getElementById("post-title").value.trim() || null;
  const body = document.getElementById("post-body").value.trim();
  const mediaInput = document.getElementById("post-media-url");
  const mediaVal = mediaInput ? mediaInput.value.trim() : "";
  const scheduledRaw = document.getElementById("scheduled-datetime").value;
  const platforms = getSelectedPlatforms();

  let hasError = false;
  if (!body) {
    showFieldError("post-body-error", "Please write your post message content.");
    hasError = true;
  }
  if (platforms.length === 0) {
    showFieldError("platform-error", "Please select at least one platform target.");
    hasError = true;
  }
  if (platforms.includes("instagram") && !mediaVal) {
    showFieldError("post-media-error", "Instagram requires a public image or video URL.");
    hasError = true;
  }
  if (!isDraft && !scheduledRaw) {
    showFieldError("datetime-error", "Please choose a scheduled time.");
    hasError = true;
  }

  if (hasError) return;

  const btn = isDraft
    ? document.getElementById("save-draft-btn")
    : document.getElementById("submit-post-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = isDraft ? "Saving draft..." : "Scheduling...";
  }

  const payload = {
    title: title,
    body: body,
    media_url: mediaVal || null,
    is_draft: isDraft,
    targets: platforms.map((p) => ({
      platform: p,
      scheduled_at: scheduledRaw || new Date().toISOString(),
    })),
  };

  try {
    const resp = await fetch("/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const errorData = await resp.json();
      throw new Error(errorData.detail || "Failed to save post");
    }

    const created = await resp.json();
    const toastMsg = isDraft ? "Draft saved successfully" : "Post scheduled successfully";
    window.location.href = `/posts/${created.id}/view?toast=${encodeURIComponent(toastMsg)}`;
  } catch (err) {
    showFieldError("form-general-error", err.message);
    if (btn) {
      btn.disabled = false;
      btn.textContent = isDraft ? "Save as draft" : "Schedule post";
    }
  }
}

// Edit Post Page Submission & Live Preview
async function submitEditPost(event, postId) {
  event.preventDefault();
  const title = document.getElementById("edit-post-title") ? document.getElementById("edit-post-title").value.trim() : null;
  const body = document.getElementById("edit-post-body").value.trim();
  const mediaInput = document.getElementById("edit-post-media-url");
  const media_url = mediaInput ? mediaInput.value.trim() : null;

  // Selected platforms
  const platforms = [];
  document.querySelectorAll(".edit-platform-checkbox:checked").forEach((cb) => {
    platforms.push(cb.value);
  });

  if (!body) {
    alert("Post content cannot be empty.");
    return;
  }
  if (platforms.length === 0) {
    alert("Please select at least one platform target.");
    return;
  }

  const saveBtn = document.getElementById("save-edit-btn");
  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
  }

  try {
    const resp = await fetch(`/posts/${postId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title: title || null,
        body: body,
        media_url: media_url || null,
        platforms: platforms,
      }),
    });

    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Edit failed");
    }

    window.location.href = `/posts/${postId}/view?toast=Post%20updated%20successfully`;
  } catch (e) {
    alert(`Error: ${e.message}`);
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save changes";
    }
  }
}

// Setup live preview on Edit page if loaded
document.addEventListener("DOMContentLoaded", () => {
  const editBody = document.getElementById("edit-post-body");
  const editTitle = document.getElementById("edit-post-title");
  const editMedia = document.getElementById("edit-post-media-url");

  if (!editBody) return;

  function updateEditPreview() {
    const bodyVal = editBody.value;
    const titleVal = editTitle ? editTitle.value : "";
    const mediaVal = editMedia ? editMedia.value : "";

    const previewSingle = document.getElementById("edit-preview-single-container");
    const previewThread = document.getElementById("edit-preview-thread-container");
    const previewTitle = document.getElementById("edit-preview-title-text");
    const previewBody = document.getElementById("edit-preview-body-text");
    const previewMediaImg = document.getElementById("edit-preview-media-img");
    const previewMediaWrap = document.getElementById("edit-preview-media-container");

    if (previewMediaWrap && previewMediaImg) {
      if (mediaVal && (mediaVal.startsWith("http://") || mediaVal.startsWith("https://"))) {
        previewMediaImg.src = mediaVal;
        previewMediaWrap.style.display = "block";
      } else {
        previewMediaWrap.style.display = "none";
      }
    }

    // Thread parts check
    const rawParts = bodyVal.split(/\n\s*---\s*\n/);
    const threadParts = rawParts.map((p) => p.trim()).filter((p) => p.length > 0);

    if (threadParts.length > 1) {
      if (previewSingle) previewSingle.style.display = "none";
      if (previewThread) {
        previewThread.style.display = "flex";
        previewThread.style.flexDirection = "column";
        previewThread.style.gap = "0.75rem";
        previewThread.innerHTML = threadParts
          .map((part, i) => {
            const isOver = part.length > 500;
            return `
              <div class="thread-chain-item" style="${isOver ? 'border-color: var(--status-failed-text);' : ''}">
                <div class="thread-part-badge" style="${isOver ? 'color: var(--status-failed-text);' : ''}">
                  Part ${i + 1} of ${threadParts.length} &bull; ${part.length}/500 chars ${isOver ? '(EXCEEDED)' : ''}
                </div>
                <div>${escapeHtml(part)}</div>
              </div>
            `;
          })
          .join("");
      }
    } else {
      if (previewThread) previewThread.style.display = "none";
      if (previewSingle) previewSingle.style.display = "block";
      if (previewTitle) {
        if (titleVal) {
          previewTitle.textContent = titleVal;
          previewTitle.style.display = "block";
        } else {
          previewTitle.style.display = "none";
        }
      }
      if (previewBody) {
        previewBody.textContent = bodyVal || "Preview will appear here as you type...";
        previewBody.style.color = bodyVal ? "inherit" : "var(--text-muted)";
      }
    }

    // Platform meters
    document.querySelectorAll(".edit-platform-checkbox").forEach((cb) => {
      const p = cb.value;
      const meter = document.getElementById(`edit-meter-${p}`);
      if (!meter) return;
      if (!cb.checked) {
        meter.style.display = "none";
        return;
      }
      meter.style.display = "inline";
      meter.classList.remove("warning", "danger");
      const max = PLATFORM_LIMITS[p] || 2000;
      const len = bodyVal.length;
      meter.textContent = `${p.toUpperCase()}: ${len} / ${max}`;
      if (len > max) meter.classList.add("danger");
      else if (len >= max * 0.8) meter.classList.add("warning");
    });
  }

  editBody.addEventListener("input", updateEditPreview);
  if (editTitle) editTitle.addEventListener("input", updateEditPreview);
  if (editMedia) editMedia.addEventListener("input", updateEditPreview);
  document.querySelectorAll(".edit-platform-checkbox").forEach((cb) => {
    cb.addEventListener("change", (e) => {
      const chip = e.target.closest(".platform-chip");
      if (chip) chip.classList.toggle("selected", e.target.checked);
      updateEditPreview();
    });
  });

  updateEditPreview();
});

// Bulk Actions Toolbar Logic
function toggleSelectAllPosts(masterCheckbox) {
  const checkboxes = document.querySelectorAll(".post-checkbox");
  checkboxes.forEach((cb) => {
    cb.checked = masterCheckbox.checked;
  });
  updateBulkToolbarState();
}

function updateBulkToolbarState() {
  const selected = document.querySelectorAll(".post-checkbox:checked");
  const count = selected.length;
  const toolbar = document.getElementById("bulk-toolbar");
  const countEl = document.getElementById("bulk-selected-count");

  if (countEl) countEl.textContent = count;
  if (toolbar) {
    toolbar.style.display = count > 0 ? "flex" : "none";
  }
}

function getSelectedPostTargetIds() {
  const ids = [];
  document.querySelectorAll(".post-checkbox:checked").forEach((cb) => {
    ids.push(parseInt(cb.value, 10));
  });
  return ids;
}

async function executeBulk(action, shiftHours = 0) {
  const targetIds = getSelectedPostTargetIds();
  if (targetIds.length === 0) return;

  if (action === "delete" && !confirm(`Move ${targetIds.length} item(s) to Trash?`)) return;

  try {
    const resp = await fetch("/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: action,
        target_ids: targetIds,
        shift_hours: shiftHours,
      }),
    });
    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Bulk action failed");
    }
    const result = await resp.json();
    showToast(`Bulk ${action}: ${result.done} completed, ${result.skipped} skipped`);
    setTimeout(() => window.location.reload(), 700);
  } catch (err) {
    showToast(`Error: ${err.message}`);
  }
}

function shiftBulkSchedulePrompt() {
  const hoursStr = prompt("Enter hours to shift schedule forward (+ hours):", "1");
  if (!hoursStr) return;
  const hours = parseInt(hoursStr, 10);
  if (isNaN(hours) || hours <= 0) {
    alert("Please enter a valid positive number of hours.");
    return;
  }
  executeBulk("shift_schedule", hours);
}

// Dashboard Failed Bulk Actions
async function retryAllFailedTargets() {
  const items = document.querySelectorAll(".btn-retry-target, [onclick*='retryTarget']");
  const targetIds = [];
  document.querySelectorAll("[id^='failed-item-']").forEach((el) => {
    const idStr = el.id.replace("failed-item-", "");
    targetIds.push(parseInt(idStr, 10));
  });
  if (targetIds.length === 0) return;

  try {
    const resp = await fetch("/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "retry", target_ids: targetIds }),
    });
    if (!resp.ok) throw new Error("Retry all failed");
    showToast(`Retried ${targetIds.length} failed target(s)`);
    setTimeout(() => window.location.reload(), 600);
  } catch (e) {
    showToast(`Error: ${e.message}`);
  }
}

async function deleteAllFailedTargets() {
  const targetIds = [];
  document.querySelectorAll("[id^='failed-item-']").forEach((el) => {
    const idStr = el.id.replace("failed-item-", "");
    targetIds.push(parseInt(idStr, 10));
  });
  if (targetIds.length === 0) return;
  if (!confirm(`Move all ${targetIds.length} failed targets to Trash?`)) return;

  try {
    const resp = await fetch("/bulk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ action: "delete", target_ids: targetIds }),
    });
    if (!resp.ok) throw new Error("Delete failed targets failed");
    showToast(`Moved ${targetIds.length} failed target(s) to Trash`);
    setTimeout(() => window.location.reload(), 600);
  } catch (e) {
    showToast(`Error: ${e.message}`);
  }
}

// Global Publishing Queue Control
async function togglePublishingPause() {
  try {
    const statusResp = await fetch("/queue/status");
    const { paused } = await statusResp.json();
    if (paused) {
      await resumePublishing();
    } else {
      await pausePublishing();
    }
  } catch (e) {
    showToast(`Queue toggle error: ${e.message}`);
  }
}

async function pausePublishing() {
  try {
    const resp = await fetch("/queue/pause", { method: "POST" });
    if (resp.ok) {
      showToast("Publishing paused. Scheduled posts will wait.");
      setTimeout(() => window.location.reload(), 500);
    }
  } catch (e) {
    showToast(`Error: ${e.message}`);
  }
}

async function resumePublishing() {
  try {
    const resp = await fetch("/queue/resume", { method: "POST" });
    if (resp.ok) {
      showToast("Publishing resumed. Overdue posts were staggered.");
      setTimeout(() => window.location.reload(), 500);
    }
  } catch (e) {
    showToast(`Error: ${e.message}`);
  }
}

// Composer Save as Draft Helper
async function submitComposerPost(isDraft = false) {
  const form = document.getElementById("composer-form");
  if (!form) return;

  clearFieldErrors();
  const title = document.getElementById("post-title").value.trim() || null;
  const body = document.getElementById("post-body").value.trim();
  const mediaInput = document.getElementById("post-media-url");
  const mediaVal = mediaInput ? mediaInput.value.trim() : "";
  const scheduledRaw = document.getElementById("scheduled-datetime").value;
  const platforms = getSelectedPlatforms();

  let hasError = false;
  if (!body) {
    showFieldError("post-body-error", "Please write your post message content.");
    hasError = true;
  }
  if (platforms.length === 0) {
    showFieldError("platform-error", "Please select at least one platform target.");
    hasError = true;
  }
  if (platforms.includes("instagram") && !mediaVal) {
    showFieldError("post-media-error", "Instagram requires a public image or video URL.");
    hasError = true;
  }
  if (!isDraft && !scheduledRaw) {
    showFieldError("datetime-error", "Please choose a scheduled time.");
    hasError = true;
  }

  if (hasError) return;

  const btn = isDraft
    ? document.getElementById("save-draft-btn")
    : document.getElementById("submit-post-btn");
  if (btn) {
    btn.disabled = true;
    btn.textContent = isDraft ? "Saving draft..." : "Scheduling...";
  }

  const payload = {
    title: title,
    body: body,
    media_url: mediaVal || null,
    is_draft: isDraft,
    targets: platforms.map((p) => ({
      platform: p,
      scheduled_at: scheduledRaw || new Date().toISOString(),
    })),
  };

  try {
    const resp = await fetch("/posts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });

    if (!resp.ok) {
      const errorData = await resp.json();
      throw new Error(errorData.detail || "Failed to save post");
    }

    const created = await resp.json();
    const toastMsg = isDraft ? "Draft saved successfully" : "Post scheduled successfully";
    window.location.href = `/posts/${created.id}/view?toast=${encodeURIComponent(toastMsg)}`;
  } catch (err) {
    showFieldError("form-general-error", err.message);
    if (btn) {
      btn.disabled = false;
      btn.textContent = isDraft ? "Save as draft" : "Schedule post";
    }
  }
}

// Edit Post Page Submission
async function submitEditPost(event, postId) {
  event.preventDefault();
  const title = document.getElementById("edit-title").value.trim() || null;
  const body = document.getElementById("edit-body").value.trim();
  const media_url = document.getElementById("edit-media-url").value.trim() || null;
  const saveBtn = document.getElementById("save-edit-btn");

  if (saveBtn) {
    saveBtn.disabled = true;
    saveBtn.textContent = "Saving...";
  }

  try {
    const resp = await fetch(`/posts/${postId}`, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, body, media_url }),
    });

    if (!resp.ok) {
      const err = await resp.json();
      throw new Error(err.detail || "Edit failed");
    }

    window.location.href = `/posts/${postId}/view?toast=Post%20updated%20successfully`;
  } catch (e) {
    alert(`Error: ${e.message}`);
    if (saveBtn) {
      saveBtn.disabled = false;
      saveBtn.textContent = "Save changes";
    }
  }
}

function updateEditCharCount(textarea) {
  const counter = document.getElementById("edit-char-counter");
  if (counter) {
    counter.textContent = `${textarea.value.length} characters`;
  }
}


