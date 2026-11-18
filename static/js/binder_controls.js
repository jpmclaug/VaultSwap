/**
 * VaultSwap — Server-driven Binder & Pod Feed UI Controls
 * View Switching, Focus Mode, Pill Highlights, and Bulk Selection Sync
 */

let binderFocusMode = false;

function triggerFilterSubmit() {
    const form = document.getElementById("binder-filters");
    if (form && window.htmx) {
        window.htmx.trigger(form, "change");
    }
}

function setBinderViewMode(mode) {
    const valid = ["rows", "art", "checklist"];
    if (!valid.includes(mode)) mode = "rows";
    localStorage.setItem("vaultswap_view_mode", mode);

    const viewInput = document.getElementById("filter-input-view");
    if (viewInput && viewInput.value !== mode) {
        viewInput.value = mode;
        updateViewSwitcherButtons(mode);
        triggerFilterSubmit();
    } else {
        updateViewSwitcherButtons(mode);
    }
}

function updateViewSwitcherButtons(mode) {
    const btnMap = {
        rows: document.getElementById("btn-view-rows"),
        art: document.getElementById("btn-view-art"),
        checklist: document.getElementById("btn-view-checklist"),
    };
    Object.entries(btnMap).forEach(([k, btn]) => {
        if (!btn) return;
        if (k === mode) {
            btn.classList.add("bg-emerald-500", "text-slate-950", "font-bold");
            btn.classList.remove("text-slate-400", "hover:text-white");
        } else {
            btn.classList.remove("bg-emerald-500", "text-slate-950", "font-bold");
            btn.classList.add("text-slate-400", "hover:text-white");
        }
    });
}

function toggleBinderFocusMode() {
    binderFocusMode = !binderFocusMode;
    localStorage.setItem("vaultswap_focus_mode", binderFocusMode ? "1" : "0");
    applyBinderFocusMode();
}

function applyBinderFocusMode() {
    const btn = document.getElementById("btn-focus-mode");
    const label = document.getElementById("focus-btn-label");
    if (binderFocusMode) {
        document.body.classList.add("binder-focus-mode");
        if (btn) {
            btn.classList.add("bg-amber-400/20", "border-amber-400/50", "text-amber-300");
            btn.classList.remove("bg-slate-950", "border-slate-800", "text-slate-300");
        }
        if (label) label.textContent = "Focus: ON";
    } else {
        document.body.classList.remove("binder-focus-mode");
        if (btn) {
            btn.classList.remove("bg-amber-400/20", "border-amber-400/50", "text-amber-300");
            btn.classList.add("bg-slate-950", "border-slate-800", "text-slate-300");
        }
        if (label) label.textContent = "Focus: OFF";
    }
}

function filterBinderColor(color) {
    const norm = (color || "all").toLowerCase().trim();
    const input = document.getElementById("filter-input-color");
    if (input) input.value = norm;
    updateColorPills(norm);
    triggerFilterSubmit();
}

function updateColorPills(activeColor) {
    const norm = (activeColor || "all").toLowerCase().trim();
    document.querySelectorAll(".binder-color-pill").forEach((p) => {
        const c = (p.dataset.color || "").toLowerCase().trim();
        if (c === norm) {
            p.classList.add("ring-2", "ring-emerald-500", "bg-slate-800", "text-white");
            p.classList.remove("bg-slate-950");
        } else {
            p.classList.remove("ring-2", "ring-emerald-500", "bg-slate-800", "text-white");
            p.classList.add("bg-slate-950");
        }
    });
}

function filterBinderType(type) {
    const norm = (type || "all").toLowerCase().trim();
    const input = document.getElementById("filter-input-type");
    if (input) input.value = norm;
    updateTypePills(norm);
    triggerFilterSubmit();
}

function updateTypePills(activeType) {
    const norm = (activeType || "all").toLowerCase().trim();
    document.querySelectorAll(".binder-type-pill").forEach((p) => {
        const t = (p.dataset.type || "").toLowerCase().trim();
        if (t === norm) {
            p.classList.add("bg-sky-500/20", "border-sky-500/50", "text-sky-300", "font-bold");
            p.classList.remove("bg-slate-950", "border-slate-800", "text-slate-400");
        } else {
            p.classList.remove("bg-sky-500/20", "border-sky-500/50", "text-sky-300", "font-bold");
            p.classList.add("bg-slate-950", "border-slate-800", "text-slate-400");
        }
    });
}

function syncToolbarPillsFromInputs() {
    const view = document.getElementById("filter-input-view")?.value || "rows";
    const color = document.getElementById("filter-input-color")?.value || "all";
    const type = document.getElementById("filter-input-type")?.value || "all";
    updateViewSwitcherButtons(view);
    updateColorPills(color);
    updateTypePills(type);

    const listEl = document.getElementById("binder-list");
    const selectAllBtn = document.getElementById("bulk-select-all-btn");
    if (listEl && selectAllBtn && listEl.dataset.totalMatches !== undefined) {
        selectAllBtn.textContent = `Select all ${listEl.dataset.totalMatches} matching`;
    }
}

document.addEventListener("DOMContentLoaded", () => {
    binderFocusMode = localStorage.getItem("vaultswap_focus_mode") === "1";
    applyBinderFocusMode();

    const savedView = localStorage.getItem("vaultswap_view_mode");
    const viewInput = document.getElementById("filter-input-view");
    const urlParams = new URLSearchParams(window.location.search);
    if (savedView && !urlParams.has("view") && viewInput && viewInput.value !== savedView) {
        setBinderViewMode(savedView);
    }
    syncToolbarPillsFromInputs();
});

document.body.addEventListener("htmx:afterSwap", () => {
    syncToolbarPillsFromInputs();
    if (typeof bulkModeActive !== "undefined" && bulkModeActive && typeof applyBulkCheckboxVisibility === "function") {
        applyBulkCheckboxVisibility();
    }
});
