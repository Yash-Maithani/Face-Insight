/* ==========================================================================
   review.js - Review tab
   Matches that came close to the recognition threshold wait here. Confirm
   one and it counts like any other record; reject it and it never counts.
   Both decisions are kept, with who made them, and audited.
   ========================================================================== */

(function () {
    "use strict";

    const list = document.getElementById("reviewList");
    const stateButtons = document.querySelectorAll("#reviewStates [data-state]");

    const EMPTY = {
        pending: "Nothing is waiting for review.",
        confirmed: "No matches have been confirmed yet.",
        rejected: "No matches have been rejected yet.",
    };

    let state = "pending";
    let seq = 0;

    function card(r) {
        const esc = App.escapeHtml;
        const distance = Number(r.confidence);
        const limit = App.getThreshold();
        const photo = r.has_snapshot
            ? `<img class="review-card__photo" src="/api/attendance/${encodeURIComponent(r.id)}/snapshot" alt="Face seen at ${esc(r.time_in || "")}">`
            : '<div class="review-card__photo review-card__photo--none">No face crop</div>';
        const decided = r.reviewed_by
            ? `<p class="review-card__decided">${r.review === "confirmed" ? "Confirmed" : "Rejected"} by ${esc(r.reviewed_by)}, ${esc(String(r.reviewed_at || "").replace("T", " ").slice(0, 16))}</p>`
            : "";
        const actions = r.review === "pending"
            ? `<div class="review-card__actions">
                   <button type="button" class="btn btn--primary btn--sm" data-decision="confirm">It is ${esc(r.name)}</button>
                   <button type="button" class="btn btn--ghost btn--sm" data-decision="reject">Not them</button>
               </div>`
            : "";
        return `
            <article class="card review-card" data-id="${esc(r.id)}">
                ${photo}
                <div class="review-card__body">
                    <h3 class="review-card__name">${esc(r.name)} <code>${r.person_id}</code></h3>
                    <p class="review-card__meta mono">${esc(r.date || "")} ${esc(r.time_in || "")}</p>
                    <p class="review-card__meta">Distance <span class="mono">${isNaN(distance) ? "--" : distance.toFixed(1)}</span>, limit ${esc(String(limit))}. Lower is closer.</p>
                    <p class="review-card__meta">Would count as <strong>${esc(r.status)}</strong>.</p>
                    ${decided}
                    ${actions}
                </div>
            </article>`;
    }

    async function load() {
        const mine = ++seq;
        App.renderState(list, "loading", "Loading...");
        let res;
        let data = {};
        try {
            res = await fetch(`/api/review?state=${state}`);
            data = await res.json().catch(() => ({}));
        } catch (err) {
            res = null;
        }
        if (mine !== seq) return;
        if (!res || !res.ok) {
            App.renderState(list, "error",
                res && res.status === 503 ? "Database offline. The review queue cannot be loaded." : "Could not load the review queue.",
                load);
            return;
        }
        if (!data.records.length) {
            App.renderState(list, "empty", EMPTY[state]);
            return;
        }
        delete list.dataset.state;
        list.innerHTML = data.records.map(card).join("");
    }

    list.addEventListener("click", async (e) => {
        const btn = e.target.closest("[data-decision]");
        if (!btn) return;
        const item = btn.closest(".review-card");
        item.querySelectorAll("button").forEach((b) => (b.disabled = true));
        try {
            const res = await fetch(`/api/review/${encodeURIComponent(item.dataset.id)}`, {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ decision: btn.dataset.decision }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok && res.status !== 404) {
                App.showToast(data.error || "The decision was not saved.", "error");
                item.querySelectorAll("button").forEach((b) => (b.disabled = false));
                return;
            }
            if (res.status === 404) {
                App.showToast("Someone else already decided this one.", "info");
            } else {
                App.showToast(btn.dataset.decision === "confirm" ? "Confirmed. It counts now." : "Rejected. It will not count.", "success");
            }
            item.remove();
            if (!list.querySelector(".review-card")) App.renderState(list, "empty", EMPTY[state]);
        } catch (err) {
            App.showToast("Could not reach the server. Nothing was changed.", "error");
            item.querySelectorAll("button").forEach((b) => (b.disabled = false));
        }
    });

    stateButtons.forEach((btn) => {
        btn.addEventListener("click", () => {
            state = btn.dataset.state;
            stateButtons.forEach((b) => b.setAttribute("aria-pressed", String(b === btn)));
            load();
        });
    });

    const isOpen = () => document.getElementById("panel-review").classList.contains("active");

    window.addEventListener("tab:navigate", (e) => {
        if (e.detail && e.detail.tab === "review") load();
    });

    // New matches arrive while the queue is open
    App.socket.on("attendance:new", (record) => {
        if (isOpen() && state === "pending" && record && record.review === "pending") load();
    });

    // Another browser decided one
    App.socket.on("review:update", (data) => {
        if (!isOpen()) return;
        const item = list.querySelector(`.review-card[data-id="${CSS.escape(data.id)}"]`);
        if (item && state === "pending") {
            item.remove();
            if (!list.querySelector(".review-card")) App.renderState(list, "empty", EMPTY[state]);
        } else if (state === data.review) {
            load();
        }
    });
})();
