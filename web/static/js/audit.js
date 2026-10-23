/* ==========================================================================
   audit.js - Audit log tab
   Who signed in, and who changed or deleted what. Filters by user, action
   and date range; newest first.
   ========================================================================== */

(function () {
    "use strict";

    const userSelect = document.getElementById("auditUser");
    const actionSelect = document.getElementById("auditAction");
    const fromInput = document.getElementById("auditFrom");
    const toInput = document.getElementById("auditTo");
    const btnReset = document.getElementById("btnAuditReset");
    const table = document.getElementById("auditTable");
    const tableBody = document.getElementById("auditTableBody");
    const emptyEl = document.getElementById("auditEmpty");
    const note = document.getElementById("auditNote");

    let actionLabels = {};
    let filtersFilled = false;
    let requestSeq = 0;

    // Details are stored as a small object; show them as "key: value" pairs
    function describeDetails(details) {
        if (!details || typeof details !== "object") return "";
        return Object.entries(details)
            .map(([key, value]) => {
                const text = Array.isArray(value) ? (value.length ? value.join(", ") : "none") : String(value);
                return `${key.replace(/_/g, " ")}: ${text}`;
            })
            .join("; ");
    }

    function fillFilters(users, actions) {
        if (filtersFilled) return;
        filtersFilled = true;
        users.forEach((name) => userSelect.appendChild(new Option(name, name)));
        Object.entries(actions)
            .sort((a, b) => a[1].localeCompare(b[1]))
            .forEach(([key, label]) => actionSelect.appendChild(new Option(label, key)));
    }

    function showMessage(kind, text, retry) {
        table.hidden = true;
        emptyEl.hidden = false;
        App.renderState(emptyEl, kind, text, retry);
    }

    function render(data) {
        const notes = [];
        if (!data.db_available) {
            notes.push("Database offline: only entries recorded during the outage are shown.");
        }
        if (data.total > data.entries.length) {
            notes.push(`Showing the newest ${data.entries.length} of ${data.total} entries. Narrow the filters to see older ones.`);
        }
        note.textContent = notes.join(" ");
        note.hidden = notes.length === 0;

        if (data.entries.length === 0) {
            const filtered = userSelect.value || actionSelect.value || fromInput.value || toInput.value;
            showMessage("empty", filtered ? "No entries match these filters." : "Nothing has been recorded yet.");
            return;
        }

        tableBody.innerHTML = data.entries
            .map((e) => `
                <tr>
                    <td data-label="Time" class="mono">${App.escapeHtml((e.time || "").replace("T", " "))}</td>
                    <td data-label="User">${App.escapeHtml(e.user || "--")}</td>
                    <td data-label="Action">${App.escapeHtml(actionLabels[e.action] || e.action)}</td>
                    <td data-label="Target" class="mono">${App.escapeHtml(e.target || "--")}</td>
                    <td data-label="Details" class="audit-details">${App.escapeHtml(describeDetails(e.details)) || "--"}</td>
                </tr>`)
            .join("");
        emptyEl.hidden = true;
        table.hidden = false;
    }

    async function load() {
        const seq = ++requestSeq;
        showMessage("loading", "Loading...");
        const params = new URLSearchParams();
        if (userSelect.value) params.set("user", userSelect.value);
        if (actionSelect.value) params.set("action", actionSelect.value);
        if (fromInput.value) params.set("from", fromInput.value);
        if (toInput.value) params.set("to", toInput.value);
        try {
            const res = await fetch("/api/audit?" + params.toString());
            if (!res.ok) throw new Error("audit " + res.status);
            const data = await res.json();
            if (seq !== requestSeq) return; // a newer request is on its way
            actionLabels = data.actions || {};
            fillFilters(data.users || [], actionLabels);
            render(data);
        } catch (err) {
            if (seq !== requestSeq) return;
            note.hidden = true;
            showMessage("error", "Could not load the audit log.", load);
        }
    }

    [userSelect, actionSelect, fromInput, toInput].forEach((el) => el.addEventListener("change", load));

    btnReset.addEventListener("click", () => {
        userSelect.value = "";
        actionSelect.value = "";
        fromInput.value = "";
        toInput.value = "";
        load();
    });

    // Load when the tab opens, so the list is current
    window.addEventListener("tab:navigate", (e) => {
        if (e.detail && e.detail.tab === "audit") load();
    });
})();
