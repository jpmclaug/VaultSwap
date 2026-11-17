// =========================================================================
// VAULTSWAP // IN-PERSON & BINDER CLIENT CONTROLS
// =========================================================================
// Handles instant client-side filtering, multi-attribute sorting,
// dynamic section grouping with counters, view switcher (rows, art, checklist),
// and Focus mode across My Binder (/binder) and Pod Feed (/pod).

let activeBinderViewMode = localStorage.getItem("vaultswap_view_mode") || "rows";
let activeBinderGroupBy = localStorage.getItem("vaultswap_groupby") || "none";
let activeBinderColor = "all";
let activeBinderType = "all";
let activeBinderFinish = "all";
let isBinderFocus = localStorage.getItem("vaultswap_focus") === "true";

function setBinderViewMode(mode) {
    activeBinderViewMode = mode || "rows";
    localStorage.setItem("vaultswap_view_mode", activeBinderViewMode);

    // Update buttons
    const btns = {
        rows: document.getElementById("btn-view-rows"),
        art: document.getElementById("btn-view-art"),
        checklist: document.getElementById("btn-view-checklist")
    };
    Object.keys(btns).forEach(k => {
        if (btns[k]) {
            if (k === activeBinderViewMode) {
                btns[k].classList.add("bg-emerald-500", "text-slate-950", "font-bold");
                btns[k].classList.remove("text-slate-400");
            } else {
                btns[k].classList.remove("bg-emerald-500", "text-slate-950", "font-bold");
                btns[k].classList.add("text-slate-400");
            }
        }
    });

    // Update view containers for binder and pod
    const views = [
        { key: "rows", ids: ["binder-rows-container", "pod-rows-container"] },
        { key: "art", ids: ["binder-art-grid", "pod-art-grid"] },
        { key: "checklist", ids: ["binder-checklist", "pod-checklist"] }
    ];

    views.forEach(v => {
        const isTarget = (v.key === activeBinderViewMode);
        v.ids.forEach(id => {
            const el = document.getElementById(id);
            if (el) {
                el.classList.toggle("hidden", !isTarget);
            }
        });
    });

    renderBinderGroups();
    updateGroupHeadersVisibility();
}

function toggleBinderFocusMode() {
    isBinderFocus = !isBinderFocus;
    localStorage.setItem("vaultswap_focus", isBinderFocus ? "true" : "false");
    applyBinderFocusMode();
}

function applyBinderFocusMode() {
    const focusBtn = document.getElementById("btn-focus-mode");
    const focusLabel = document.getElementById("focus-btn-label");

    if (isBinderFocus) {
        document.body.classList.add("binder-focus-mode");
        if (focusLabel) focusLabel.textContent = "Focus: ON";
        if (focusBtn) {
            focusBtn.classList.add("border-emerald-500", "text-emerald-300", "bg-emerald-500/15");
        }
    } else {
        document.body.classList.remove("binder-focus-mode");
        if (focusLabel) focusLabel.textContent = "Focus: OFF";
        if (focusBtn) {
            focusBtn.classList.remove("border-emerald-500", "text-emerald-300", "bg-emerald-500/15");
        }
    }
}

function filterBinderColor(color) {
    activeBinderColor = (color || "all").toLowerCase().trim();
    const pills = document.querySelectorAll(".binder-color-pill");
    pills.forEach(p => {
        const c = (p.dataset.color || "").toLowerCase().trim();
        if (c === activeBinderColor) {
            p.classList.add("border-emerald-400", "bg-emerald-500/20", "text-white");
            p.classList.remove("border-slate-800", "text-slate-400");
        } else {
            p.classList.remove("border-emerald-400", "bg-emerald-500/20", "text-white");
            p.classList.add("border-slate-800", "text-slate-400");
        }
    });
    applyClientFilteringAndSorting();
}

function filterBinderType(type) {
    activeBinderType = (type || "all").toLowerCase().trim();
    const pills = document.querySelectorAll(".binder-type-pill");
    pills.forEach(p => {
        const t = (p.dataset.type || "").toLowerCase().trim();
        if (t === activeBinderType) {
            p.classList.add("border-emerald-400", "bg-emerald-500/20", "text-white");
            p.classList.remove("border-slate-800", "text-slate-400");
        } else {
            p.classList.remove("border-emerald-400", "bg-emerald-500/20", "text-white");
            p.classList.add("border-slate-800", "text-slate-400");
        }
    });
    applyClientFilteringAndSorting();
}

function filterBinderFinish(finish) {
    activeBinderFinish = (finish || "all").toLowerCase().trim();
    const select = document.getElementById("binder-finish-select");
    if (select && select.value !== activeBinderFinish) {
        select.value = activeBinderFinish;
    }
    applyClientFilteringAndSorting();
}

function setBinderGroupBy(mode) {
    activeBinderGroupBy = mode || "none";
    localStorage.setItem("vaultswap_groupby", activeBinderGroupBy);
    const select = document.getElementById("binder-group-select");
    if (select && select.value !== activeBinderGroupBy) {
        select.value = activeBinderGroupBy;
    }
    renderBinderGroups();
    updateGroupHeadersVisibility();
}

function sortBinderItems(mode) {
    const sortSelect = document.getElementById("binder-sort-select");
    const activeSort = mode || (sortSelect ? sortSelect.value : "default");

    const containers = [
        document.getElementById("binder-rows-container"),
        document.getElementById("binder-art-grid"),
        document.getElementById("binder-checklist"),
        document.getElementById("pod-rows-container"),
        document.getElementById("pod-art-grid"),
        document.getElementById("pod-checklist")
    ];

    containers.forEach(container => {
        if (!container) return;
        const items = Array.from(container.children).filter(el => el.classList.contains("vault-card-item"));
        if (items.length <= 1) return;

        items.sort((a, b) => {
            const priceA = parseFloat(a.dataset.price ?? "999999");
            const priceB = parseFloat(b.dataset.price ?? "999999");
            const valA = parseFloat(a.dataset.totalValue ?? "0");
            const valB = parseFloat(b.dataset.totalValue ?? "0");
            const credA = parseFloat(a.dataset.meepleCredit ?? "0");
            const credB = parseFloat(b.dataset.meepleCredit ?? "0");
            const cmcA = parseFloat(a.dataset.cmc ?? "0");
            const cmcB = parseFloat(b.dataset.cmc ?? "0");
            const qtyA = parseInt(a.dataset.qty ?? "1", 10);
            const qtyB = parseInt(b.dataset.qty ?? "1", 10);
            const orderA = parseInt(a.dataset.order ?? "0", 10);
            const orderB = parseInt(b.dataset.order ?? "0", 10);
            const nameA = (a.dataset.name || "").toLowerCase();
            const nameB = (b.dataset.name || "").toLowerCase();
            const setA = (a.dataset.set || "").toLowerCase();
            const setB = (b.dataset.set || "").toLowerCase();

            if (activeSort === "price_desc") return priceB - priceA;
            if (activeSort === "price_asc") return priceA - priceB;
            if (activeSort === "total_value_desc") return valB - valA;
            if (activeSort === "meeple_credit_desc") return credB - credA;
            if (activeSort === "cmc_asc") return cmcA - cmcB;
            if (activeSort === "qty_desc") return qtyB - qtyA;
            if (activeSort === "name_asc") return nameA.localeCompare(nameB);
            if (activeSort === "name_desc") return nameB.localeCompare(nameA);
            if (activeSort === "set_asc") return setA.localeCompare(setB);
            return orderA - orderB;
        });

        items.forEach(item => container.appendChild(item));
    });

    renderBinderGroups();
    updateGroupHeadersVisibility();
}

function applyClientFilteringAndSorting() {
    const rawSearch = document.getElementById("vault-search-input")?.value || "";
    const searchVal = rawSearch.toLowerCase().trim();
    const cards = document.querySelectorAll(".vault-card-item");

    cards.forEach(card => {
        const name = (card.dataset.name || "").toLowerCase();
        const set = (card.dataset.set || "").toLowerCase();
        const owner = (card.dataset.owner || "").toLowerCase();
        const color = (card.dataset.colorBucket || "colorless").toLowerCase();
        const type = (card.dataset.primaryType || "other").toLowerCase();
        const finish = (card.dataset.finish || "nonfoil").toLowerCase();

        const matchesSearch = !searchVal || name.includes(searchVal) || set.includes(searchVal) || owner.includes(searchVal);

        let matchesColor = true;
        if (activeBinderColor && activeBinderColor !== "all") {
            matchesColor = (color === activeBinderColor);
        }

        let matchesType = true;
        if (activeBinderType && activeBinderType !== "all") {
            matchesType = (type === activeBinderType);
        }

        let matchesFinish = true;
        if (activeBinderFinish && activeBinderFinish !== "all") {
            if (activeBinderFinish === "foil" || activeBinderFinish === "etched") {
                matchesFinish = (finish === activeBinderFinish);
            } else if (activeBinderFinish === "nonfoil" || activeBinderFinish === "normal") {
                matchesFinish = (finish === "nonfoil" || finish === "normal");
            } else if (activeBinderFinish === "special") {
                matchesFinish = (finish === "foil" || finish === "etched");
            }
        }

        if (matchesSearch && matchesColor && matchesType && matchesFinish) {
            card.classList.remove("hidden");
        } else {
            card.classList.add("hidden");
        }
    });

    updateGroupHeadersVisibility();
}

function renderBinderGroups() {
    const containers = [
        { el: document.getElementById("binder-rows-container"), isGrid: false },
        { el: document.getElementById("binder-art-grid"), isGrid: true },
        { el: document.getElementById("binder-checklist"), isGrid: false },
        { el: document.getElementById("pod-rows-container"), isGrid: false },
        { el: document.getElementById("pod-art-grid"), isGrid: true },
        { el: document.getElementById("pod-checklist"), isGrid: false }
    ];

    containers.forEach(({ el, isGrid }) => {
        if (!el) return;

        // Remove existing headers
        el.querySelectorAll(".binder-group-header").forEach(h => h.remove());

        if (activeBinderGroupBy === "none") {
            return;
        }

        const cards = Array.from(el.children).filter(c => c.classList.contains("vault-card-item"));
        if (cards.length === 0) return;

        let groupOrder = [];
        let groupLabels = {};
        let groupIcons = {};

        if (activeBinderGroupBy === "color") {
            groupOrder = ["w", "u", "b", "r", "g", "multicolor", "colorless", "land", "other"];
            groupLabels = {
                w: "White (W)",
                u: "Blue (U)",
                b: "Black (B)",
                r: "Red (R)",
                g: "Green (G)",
                multicolor: "Multicolor",
                colorless: "Colorless / Artifacts",
                land: "Lands",
                other: "Other"
            };
            groupIcons = {
                w: "☀️",
                u: "💧",
                b: "💀",
                r: "🔥",
                g: "🌲",
                multicolor: "🌈",
                colorless: "⚙️",
                land: "🏔️",
                other: "🃏"
            };
        } else if (activeBinderGroupBy === "type") {
            groupOrder = ["creature", "instant", "sorcery", "artifact", "enchantment", "planeswalker", "land", "other"];
            groupLabels = {
                creature: "Creatures",
                instant: "Instants",
                sorcery: "Sorceries",
                artifact: "Artifacts",
                enchantment: "Enchantments",
                planeswalker: "Planeswalkers",
                land: "Lands",
                other: "Other Types"
            };
            groupIcons = {
                creature: "⚔️",
                instant: "⚡",
                sorcery: "📜",
                artifact: "🛡️",
                enchantment: "✨",
                planeswalker: "👑",
                land: "🏔️",
                other: "🃏"
            };
        } else if (activeBinderGroupBy === "alpha") {
            groupOrder = "ABCDEFGHIJKLMNOPQRSTUVWXYZ#".split("");
            groupOrder.forEach(letter => {
                groupLabels[letter] = letter;
                groupIcons[letter] = "🔤";
            });
        } else if (activeBinderGroupBy === "set") {
            const rawSets = new Set();
            cards.forEach(c => {
                const s = (c.dataset.set || "").toUpperCase().trim();
                if (s) rawSets.add(s);
            });
            groupOrder = Array.from(rawSets).sort();
            groupOrder.forEach(s => {
                groupLabels[s] = `Set ${s}`;
                groupIcons[s] = "📦";
            });
        } else if (activeBinderGroupBy === "status") {
            groupOrder = ["meeple_bound", "for_trade", "private"];
            groupLabels = {
                meeple_bound: "Meeple Bound (Trade or Buylist)",
                for_trade: "For Trade",
                private: "Private Binder"
            };
            groupIcons = {
                meeple_bound: "⚡",
                for_trade: "🤝",
                private: "🔒"
            };
        } else if (activeBinderGroupBy === "owner") {
            const rawOwners = new Set();
            cards.forEach(c => {
                const o = (c.dataset.owner || "").trim();
                if (o) rawOwners.add(o);
            });
            groupOrder = Array.from(rawOwners).sort();
            groupOrder.forEach(o => {
                groupLabels[o] = o;
                groupIcons[o] = "👤";
            });
        }

        const buckets = {};
        groupOrder.forEach(k => { buckets[k] = []; });

        cards.forEach(card => {
            let key = "other";
            if (activeBinderGroupBy === "color") {
                key = (card.dataset.colorBucket || "colorless").toLowerCase();
                if (!buckets[key]) key = "other";
            } else if (activeBinderGroupBy === "type") {
                key = (card.dataset.primaryType || "other").toLowerCase();
                if (!buckets[key]) key = "other";
            } else if (activeBinderGroupBy === "alpha") {
                const firstChar = (card.dataset.name || "")[0]?.toUpperCase() || "#";
                key = /^[A-Z]$/.test(firstChar) ? firstChar : "#";
            } else if (activeBinderGroupBy === "set") {
                key = (card.dataset.set || "").toUpperCase().trim();
                if (!buckets[key]) {
                    buckets[key] = [];
                    groupOrder.push(key);
                    groupLabels[key] = `Set ${key}`;
                    groupIcons[key] = "📦";
                }
            } else if (activeBinderGroupBy === "status") {
                key = (card.dataset.status || "private").toLowerCase();
                if (!buckets[key]) key = "private";
            } else if (activeBinderGroupBy === "owner") {
                key = (card.dataset.owner || "").trim();
                if (!buckets[key]) {
                    buckets[key] = [];
                    groupOrder.push(key);
                    groupLabels[key] = key;
                    groupIcons[key] = "👤";
                }
            }

            if (buckets[key]) {
                buckets[key].push(card);
            }
        });

        groupOrder.forEach(k => {
            const groupCards = buckets[k];
            if (!groupCards || groupCards.length === 0) return;

            const icon = groupIcons[k] || "❖";
            const label = groupLabels[k] || k.toUpperCase();

            const header = document.createElement("div");
            header.className = isGrid
                ? "col-span-full binder-group-header bg-slate-900/90 border-l-4 border-emerald-500 rounded-lg px-3 py-1.5 flex items-center justify-between text-xs font-semibold text-white my-1"
                : "binder-group-header bg-slate-900/90 border-l-4 border-emerald-500 rounded-lg px-3 py-1.5 flex items-center justify-between text-xs font-semibold text-white my-1";
            header.dataset.groupKey = k;
            header.innerHTML = `
                <span class="flex items-center gap-1.5 uppercase tracking-wide">
                    <span>${icon}</span>
                    <span>${label}</span>
                </span>
                <span class="text-[10px] text-slate-400 uppercase font-bold group-count-badge">
                    ${groupCards.length} card${groupCards.length === 1 ? '' : 's'}
                </span>
            `;

            el.appendChild(header);
            groupCards.forEach(c => el.appendChild(c));
        });
    });
}

function updateGroupHeadersVisibility() {
    const containers = [
        document.getElementById("binder-rows-container"),
        document.getElementById("binder-art-grid"),
        document.getElementById("binder-checklist"),
        document.getElementById("pod-rows-container"),
        document.getElementById("pod-art-grid"),
        document.getElementById("pod-checklist")
    ];

    containers.forEach(container => {
        if (!container) return;
        const headers = container.querySelectorAll(".binder-group-header");
        headers.forEach(header => {
            let nextEl = header.nextElementSibling;
            let visibleCount = 0;
            while (nextEl && !nextEl.classList.contains("binder-group-header")) {
                if (nextEl.classList.contains("vault-card-item") && !nextEl.classList.contains("hidden")) {
                    visibleCount++;
                }
                nextEl = nextEl.nextElementSibling;
            }
            if (visibleCount === 0) {
                header.classList.add("hidden");
            } else {
                header.classList.remove("hidden");
                const badge = header.querySelector(".group-count-badge");
                if (badge) {
                    badge.textContent = `${visibleCount} card${visibleCount === 1 ? '' : 's'}`;
                }
            }
        });
    });
}

// HTMX Lifecycle Hook
document.addEventListener("DOMContentLoaded", () => {
    applyBinderFocusMode();
    setBinderViewMode(activeBinderViewMode);
    if (activeBinderGroupBy && activeBinderGroupBy !== "none") {
        setBinderGroupBy(activeBinderGroupBy);
    }
});

document.body.addEventListener("htmx:afterSwap", () => {
    applyBinderFocusMode();
    setBinderViewMode(activeBinderViewMode);
    if (typeof applyBulkCheckboxVisibility === "function") {
        applyBulkCheckboxVisibility();
    }
    applyClientFilteringAndSorting();
    renderBinderGroups();
    updateGroupHeadersVisibility();
});
