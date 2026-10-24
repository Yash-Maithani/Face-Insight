/* ==========================================================================
   people.js - People tab
   The list of registered people with search, and one page per person:
   their attendance day by day over a date range, renaming and their group.
   URL: #people for the list, #people/3 for person 3.
   ========================================================================== */

(function () {
    "use strict";

    const MAX_RANGE_DAYS = 366;
    const DEFAULT_DAYS = 30;

    const listView = document.getElementById("peopleListView");
    const personView = document.getElementById("personView");
    const search = document.getElementById("peopleSearch");
    const table = document.getElementById("peopleTable");
    const tableBody = document.getElementById("peopleTableBody");
    const emptyEl = document.getElementById("peopleEmpty");

    const btnBack = document.getElementById("btnPeopleBack");
    const fromInput = document.getElementById("personFrom");
    const toInput = document.getElementById("personTo");
    const errRange = document.getElementById("errPersonRange");
    const nameEl = document.getElementById("personName");
    const idEl = document.getElementById("personId");
    const imagesEl = document.getElementById("personImages");
    const consentEl = document.getElementById("personConsent");
    const groupSelect = document.getElementById("personGroup");
    const errGroup = document.getElementById("errPersonGroup");
    const renameForm = document.getElementById("personRenameForm");
    const renameInput = document.getElementById("personNewName");
    const errRename = document.getElementById("errPersonRename");
    const kpiPresent = document.getElementById("personKpiPresent");
    const kpiLate = document.getElementById("personKpiLate");
    const kpiAbsent = document.getElementById("personKpiAbsent");
    const kpiRecords = document.getElementById("personKpiRecords");
    const daysTable = document.getElementById("personDaysTable");
    const daysBody = document.getElementById("personDaysBody");
    const daysEmpty = document.getElementById("personDaysEmpty");

    let people = [];
    let listSeq = 0;
    let detailSeq = 0;
    let currentId = null;
    let current = null; // last detail response

    const STATUS_CLASS = {
        Present: "status-badge--present",
        Late: "status-badge--late",
        Absent: "status-badge--absent",
        "Not seen yet": "status-badge--pending",
    };

    function statusBadge(status) {
        return `<span class="status-badge ${STATUS_CLASS[status] || ""}">${App.escapeHtml(status)}</span>`;
    }

    // The groups a person can be moved to
    async function loadGroups(selected) {
        try {
            const res = await fetch("/api/schedules");
            if (!res.ok) throw new Error("schedules");
            const data = await res.json();
            groupSelect.innerHTML = data.groups
                .map((g) => `<option value="${App.escapeHtml(g.name)}">${App.escapeHtml(g.name)} (late after ${App.escapeHtml(g.late_after)})</option>`)
                .join("");
            groupSelect.value = selected;
        } catch (err) {
            groupSelect.innerHTML = `<option>${App.escapeHtml(selected || "Default")}</option>`;
        }
    }

    function when(iso) {
        return iso ? App.escapeHtml(String(iso).replace("T", " ").slice(0, 16)) : "--";
    }

    // ── List ─────────────────────────────────────────────────────────────
    async function loadList() {
        const seq = ++listSeq;
        if (!people.length) {
            table.hidden = true;
            App.renderState(emptyEl, "loading", "Loading people...");
        }
        try {
            const res = await fetch("/api/people");
            if (!res.ok) throw new Error("people " + res.status);
            const data = await res.json();
            if (seq !== listSeq) return;
            people = data.people || [];
            renderList();
        } catch (err) {
            if (seq !== listSeq) return;
            people = [];
            table.hidden = true;
            App.renderState(emptyEl, "error", "Could not load the list of people.", loadList);
        }
    }

    function matches(p, q) {
        if (!q) return true;
        return p.name.toLowerCase().includes(q) || String(p.person_id) === q || (p.group || "").toLowerCase().includes(q);
    }

    function renderList() {
        const q = search.value.trim().toLowerCase();
        const shown = people.filter((p) => matches(p, q));
        if (!shown.length) {
            table.hidden = true;
            App.renderState(emptyEl, "empty", people.length
                ? `No one matches "${search.value.trim()}".`
                : "No one is registered yet. Add people on the Register tab.");
            return;
        }
        tableBody.innerHTML = shown
            .map((p) => `
                <tr tabindex="0" data-key-activate data-id="${p.person_id}">
                    <td data-label="Name"><strong>${App.escapeHtml(p.name)}</strong></td>
                    <td data-label="ID" class="col-num"><code>${p.person_id}</code></td>
                    <td data-label="Group">${App.escapeHtml(p.group || "Default")}</td>
                    <td data-label="Images" class="col-num">${p.image_count}</td>
                    <td data-label="Consent">${p.consent_at ? when(p.consent_at) : "Not recorded"}</td>
                    <td data-label="Last seen" class="mono">${p.last_seen ? App.escapeHtml(p.last_seen.slice(0, 16)) : "Never"}</td>
                </tr>`)
            .join("");
        tableBody.querySelectorAll("tr").forEach((tr) => {
            tr.addEventListener("click", () => openPerson(Number(tr.dataset.id)));
        });
        emptyEl.innerHTML = "";
        table.hidden = false;
    }

    search.addEventListener("input", renderList);

    // ── One person ───────────────────────────────────────────────────────
    function defaultRange() {
        const to = App.dates.today();
        return { from: App.dates.addDays(to, -(DEFAULT_DAYS - 1)), to };
    }

    function showList() {
        currentId = null;
        current = null;
        personView.hidden = true;
        listView.hidden = false;
        loadList();
    }

    function openPerson(id, fromRoute) {
        currentId = id;
        listView.hidden = true;
        personView.hidden = false;
        if (!fromInput.value || !toInput.value) {
            const r = defaultRange();
            fromInput.value = r.from;
            toInput.value = r.to;
        }
        fromInput.max = App.dates.today();
        toInput.max = App.dates.today();
        if (!fromRoute) App.route.setArg("people", String(id));
        loadPerson();
    }

    function setKpis(totals, records) {
        App.setKpi(kpiPresent, totals ? totals.Present : null);
        App.setKpi(kpiLate, totals ? totals.Late : null);
        App.setKpi(kpiAbsent, totals ? totals.Absent : null);
        App.setKpi(kpiRecords, records ? records.length : null);
    }

    async function loadPerson() {
        const seq = ++detailSeq;
        errRange.textContent = "";
        const from = fromInput.value;
        const to = toInput.value;
        if (!App.dates.isPast(from) || !App.dates.isPast(to)) {
            errRange.textContent = "Pick two dates, neither after today.";
            return;
        }
        if (from > to) {
            errRange.textContent = "The start date must be on or before the end date.";
            return;
        }
        if (App.dates.span(from, to) > MAX_RANGE_DAYS) {
            errRange.textContent = `A range can be at most ${MAX_RANGE_DAYS} days.`;
            return;
        }
        daysTable.hidden = true;
        App.renderState(daysEmpty, "loading", "Loading attendance...");
        setKpis(null, null);

        let res;
        let data = {};
        try {
            res = await fetch(`/api/people/${currentId}?from=${from}&to=${to}`);
            data = await res.json().catch(() => ({}));
        } catch (err) {
            res = null;
        }
        if (seq !== detailSeq) return;

        if (res && res.status === 404) {
            App.showToast("That person is no longer registered.", "info");
            App.route.setArg("people", "");
            showList();
            return;
        }
        if (data.person) renderPersonHead(data.person);
        if (!res || !res.ok) {
            const offline = res && res.status === 503;
            App.renderState(daysEmpty, "error",
                offline ? "Database offline. Attendance history cannot be loaded." : (data.error || "Could not load this person's attendance."),
                loadPerson);
            return;
        }
        current = data;
        setKpis(data.totals, data.records);
        renderDays();
    }

    function renderPersonHead(p) {
        nameEl.textContent = p.name;
        idEl.textContent = p.person_id;
        imagesEl.textContent = p.image_count !== undefined ? p.image_count : "--";
        consentEl.textContent = p.consent_at ? String(p.consent_at).replace("T", " ").slice(0, 16) : "Not recorded";
        if (document.activeElement !== renameInput) renameInput.value = p.name;
        if (p.group !== undefined && document.activeElement !== groupSelect) loadGroups(p.group);
        document.title = `${p.name} | People | Face Insight`;
    }

    function renderDays() {
        const days = current.days || [];
        if (!days.length) {
            daysTable.hidden = true;
            App.renderState(daysEmpty, "empty", "No working days and no records in this range.");
            return;
        }
        const byDate = {};
        (current.records || []).forEach((r) => {
            (byDate[r.date] = byDate[r.date] || []).push(r);
        });
        daysBody.innerHTML = days
            .map((d) => {
                const expandable = d.sightings > 0;
                return `
                <tr class="${expandable ? "day-row" : ""}" data-date="${d.date}" ${expandable ? 'tabindex="0" data-key-activate aria-expanded="false"' : ""}>
                    <td data-label="Date" class="mono">${d.date}</td>
                    <td data-label="Status">${statusBadge(d.status)}${d.left_early ? ' <span class="review-tag">Left early</span>' : ""}${d.manual ? ' <span class="manual-tag">Manual</span>' : ""}</td>
                    <td data-label="First seen" class="mono">${d.first_seen || "--"}</td>
                    <td data-label="Last seen" class="mono">${d.last_seen || "--"}</td>
                    <td data-label="Records" class="col-num">${d.sightings}</td>
                </tr>`;
            })
            .join("");
        daysBody.querySelectorAll("tr.day-row").forEach((tr) => {
            tr.addEventListener("click", () => toggleDay(tr, byDate[tr.dataset.date] || []));
        });
        daysEmpty.innerHTML = "";
        daysTable.hidden = false;
    }

    // The records behind a day, shown under its row
    function toggleDay(tr, records) {
        const open = tr.getAttribute("aria-expanded") === "true";
        const next = tr.nextElementSibling;
        if (open) {
            if (next && next.classList.contains("day-records")) next.remove();
            tr.setAttribute("aria-expanded", "false");
            return;
        }
        const detail = document.createElement("tr");
        detail.className = "day-records";
        detail.innerHTML = `<td colspan="5"><ul class="day-records__list">${records
            .sort((a, b) => (a.time_in || "").localeCompare(b.time_in || ""))
            .map((r) => `<li><span class="mono">${App.escapeHtml(r.time_in || "--")}</span>
                ${statusBadge(r.status)}
                <span>${r.manual ? "Marked by hand" : "Camera"}${r.emotion ? ", " + App.escapeHtml(r.emotion) : ""}</span></li>`)
            .join("")}</ul></td>`;
        tr.after(detail);
        tr.setAttribute("aria-expanded", "true");
    }

    [fromInput, toInput].forEach((el) => el.addEventListener("change", loadPerson));

    groupSelect.addEventListener("change", async () => {
        errGroup.textContent = "";
        const group = groupSelect.value;
        try {
            const res = await fetch(`/api/people/${currentId}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ group }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                errGroup.textContent = data.error || "The group was not changed.";
                return;
            }
            App.showToast(`Moved to ${group}. Late and Absent now follow its schedule.`, "success");
            loadPerson();
        } catch (err) {
            errGroup.textContent = "Could not reach the server.";
        }
    });

    btnBack.addEventListener("click", () => {
        App.route.setArg("people", "");
        showList();
    });

    renameForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        errRename.textContent = "";
        const name = renameInput.value.trim();
        if (!name) {
            errRename.textContent = "Name is required.";
            return;
        }
        try {
            const res = await fetch(`/api/people/${currentId}`, {
                method: "PATCH",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ name }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                errRename.textContent = data.error || "The name was not changed.";
                return;
            }
            renderPersonHead(Object.assign({}, current && current.person, data.person));
            App.showToast(`Renamed to ${data.person.name}. New records use the new name.`, "success");
        } catch (err) {
            errRename.textContent = "Could not reach the server.";
        }
    });

    // ── Routing ──────────────────────────────────────────────────────────
    function showFromArg(arg) {
        const id = /^\d+$/.test(arg || "") ? Number(arg) : null;
        if (id === null) {
            if (arg) App.route.setArg("people", "");
            showList();
        } else if (id !== currentId || personView.hidden) {
            openPerson(id, true);
        }
    }

    window.addEventListener("tab:navigate", (e) => {
        if (!e.detail || e.detail.tab !== "people") return;
        if (e.detail.opts && e.detail.opts.fromRoute) {
            showFromArg(App.route.get().arg);
        } else if (currentId !== null) {
            App.route.setArg("people", String(currentId));
            loadPerson();
        } else {
            showList();
        }
    });

    window.addEventListener("route:change", (e) => {
        if (e.detail.tab === "people") showFromArg(e.detail.arg);
    });

    // New records change "last seen" and today's row
    App.socket.on("attendance:new", () => {
        if (!document.getElementById("panel-people").classList.contains("active")) return;
        if (currentId === null) loadList();
    });
})();
