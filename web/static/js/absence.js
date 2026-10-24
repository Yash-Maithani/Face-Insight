/* ==========================================================================
   absence.js - "Not here yet" on the Live Monitor
   Registered people whose group works today and who have not been seen.
   Someone is listed once their group's late cut-off has passed.
   ========================================================================== */

(function () {
    "use strict";

    const list = document.getElementById("absenceList");
    const summary = document.getElementById("absenceSummary");
    let seq = 0;
    let timer = null;

    async function load() {
        const mine = ++seq;
        let res;
        let data = {};
        try {
            res = await fetch("/api/absent/today");
            data = await res.json().catch(() => ({}));
        } catch (err) {
            res = null;
        }
        if (mine !== seq) return;
        if (!res || !res.ok) {
            summary.textContent = "";
            App.renderState(list, "error",
                res && res.status === 503 ? "Database offline. The list cannot be worked out." : "Could not load who is missing.",
                load);
            return;
        }
        render(data);
    }

    function render(data) {
        const overdue = data.missing.filter((m) => m.overdue);
        if (!data.scheduled) {
            summary.textContent = "";
            App.renderState(list, "empty", "No one is scheduled today.");
            return;
        }
        if (!overdue.length) {
            summary.textContent = "";
            const waiting = data.missing.length;
            App.renderState(list, "empty", waiting && data.next_cutoff
                ? `${waiting} not seen yet. The list starts at ${data.next_cutoff}, the late cut-off.`
                : "Everyone scheduled today has been seen.");
            return;
        }
        summary.textContent = `${overdue.length} of ${data.scheduled}`;
        delete list.dataset.state;
        list.innerHTML = `<ul class="absence-list__items">${overdue
            .map((m) => `<li>
                <button type="button" class="absence-item" data-id="${m.person_id}" title="Open ${App.escapeHtml(m.name)}">
                    <span class="shape shape--absent" aria-hidden="true"></span>
                    <span class="absence-item__name">${App.escapeHtml(m.name)}</span>
                    <span class="absence-item__group">${App.escapeHtml(m.group)}, late after ${App.escapeHtml(m.late_after)}</span>
                </button>
            </li>`)
            .join("")}</ul>`;
    }

    list.addEventListener("click", (e) => {
        const item = e.target.closest(".absence-item");
        if (!item) return;
        // The router opens the People tab on that person
        window.location.hash = `#people/${item.dataset.id}`;
    });

    // Someone just arrived, a decision changed what counts, or a cut-off passed
    App.socket.on("attendance:new", load);
    App.socket.on("attendance:deleted", load);
    App.socket.on("review:update", load);
    window.addEventListener("schedules:change", load);
    window.addEventListener("db:status", (e) => {
        if (e.detail.connected) load();
    });

    App.renderState(list, "loading", "Loading...");
    load();
    clearInterval(timer);
    timer = setInterval(load, 60000);
})();
