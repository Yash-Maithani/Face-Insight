/* ==========================================================================
   attendance.js - Attendance management tab
   Date navigation, client-side filtering/sorting, detail drawer,
   manual mark modal, Excel/CSV export, and live WebSocket updates.
   ========================================================================== */

(function () {
    "use strict";

    // ── State ────────────────────────────────────────────────────────────
    // The shown period: one day when from === to, otherwise a date range.
    const MAX_RANGE_DAYS = 366;
    const today = () => App.dates.today();
    let range = initialRange();
    let records = [];
    let currentFilter = "All"; // "All" | "Present" | "Late" | "Unknown" | "Review"
    let currentSort = "time_desc"; // "time_desc" | "time_asc" | "name_asc" | "status_prio"
    let sortCol = null;
    let sortAsc = true;

    const isRange = () => range.from !== range.to;
    // Matches waiting for review, or rejected in review, do not count
    const counted = (r) => r.review !== "pending" && r.review !== "rejected";
    const REVIEW_TEXT = { pending: "Needs review", rejected: "Rejected in review", confirmed: "Confirmed in review" };
    const rangeKey = (r) => `${r.from}..${r.to}`;
    const inRange = (date) => date >= range.from && date <= range.to;
    const periodText = () => (isRange() ? "in this date range" : "for this date");

    // From the URL: #attendance/2026-10-07 for a day, #attendance/2026-10-01..2026-10-07 for a range
    function rangeFromArg(arg) {
        if (App.dates.isPast(arg)) return { from: arg, to: arg };
        return App.dates.parseRange(arg, MAX_RANGE_DAYS) || { from: today(), to: today() };
    }

    function initialRange() {
        const route = App.route.get();
        return rangeFromArg(route.tab === "attendance" ? route.arg : "");
    }

    function routeArg() {
        if (isRange()) return rangeKey(range);
        return range.from === today() ? "" : range.from;
    }

    function syncRoute() {
        App.route.setArg("attendance", routeArg());
    }

    // ── DOM Elements ─────────────────────────────────────────────────────
    const datePicker = document.getElementById("attendanceDatePicker");
    const dateTo = document.getElementById("attendanceDateTo");
    const dateCol = document.getElementById("attendanceDateCol");
    const btnDatePrev = document.getElementById("btnDatePrev");
    const btnDateNext = document.getElementById("btnDateNext");
    const btnDateToday = document.getElementById("btnDateToday");

    const kpiTotal = document.getElementById("kpiAttendanceTotal");
    const kpiPresent = document.getElementById("kpiAttendancePresent");
    const kpiLate = document.getElementById("kpiAttendanceLate");
    const kpiUnknown = document.getElementById("kpiAttendanceUnknown");
    const kpiCards = document.querySelectorAll(".kpi-card[data-filter]");

    const filterPillsContainer = document.getElementById("attendanceFilterPills");
    const countPillAll = document.getElementById("countPillAll");
    const countPillPresent = document.getElementById("countPillPresent");
    const countPillLate = document.getElementById("countPillLate");
    const countPillUnknown = document.getElementById("countPillUnknown");
    const countPillReview = document.getElementById("countPillReview");

    const sortSelect = document.getElementById("attendanceSortSelect");
    const table = document.getElementById("attendanceTable");
    const tableBody = document.getElementById("attendanceTableBody");
    const tableEmpty = document.getElementById("attendanceEmpty");

    const drawerOverlay = document.getElementById("attendanceDrawerOverlay");
    const drawer = document.getElementById("attendanceDrawer");
    const drawerBody = document.getElementById("attendanceDrawerBody");
    const btnDrawerClose = document.getElementById("btnDrawerClose");

    const btnManualMark = document.getElementById("btnManualMark");
    const manualModalOverlay = document.getElementById("manualMarkModalOverlay");
    const manualModalClose = document.getElementById("btnManualModalClose");
    const manualModalCancel = document.getElementById("btnManualCancel");
    const manualForm = document.getElementById("manualMarkForm");
    const manualError = document.getElementById("manualMarkError");
    const manualPerson = document.getElementById("manualPerson");
    const manualPersonHint = document.getElementById("manualPersonHint");
    const manualStatus = document.getElementById("manualStatus");
    const btnManualSubmit = document.getElementById("btnManualSubmit");

    const btnExportExcel = document.getElementById("btnExportExcel");
    const btnExportCsv = document.getElementById("btnExportCsv");

    // ── Date Navigation ──────────────────────────────────────────────────
    function setRange(from, to) {
        range = { from, to };
        datePicker.value = from;
        dateTo.value = to;
        updateDateButtonStates();
        syncRoute();
        fetchRecords();
    }

    // Previous and next move by the length of the period shown
    function shiftRange(direction) {
        const n = App.dates.span(range.from, range.to) * direction;
        const to = App.dates.addDays(range.to, n);
        if (to > today()) return;
        setRange(App.dates.addDays(range.from, n), to);
    }

    function initDateControls() {
        datePicker.value = range.from;
        dateTo.value = range.to;
        datePicker.max = today();
        dateTo.max = today();
        updateDateButtonStates();

        datePicker.addEventListener("change", () => {
            let from = datePicker.value;
            if (!App.dates.isPast(from)) from = from > today() ? today() : range.from;
            // Moving the start past the end makes it a single day
            let to = range.to < from ? from : range.to;
            if (App.dates.span(from, to) > MAX_RANGE_DAYS) to = App.dates.addDays(from, MAX_RANGE_DAYS - 1);
            setRange(from, to);
        });

        dateTo.addEventListener("change", () => {
            let to = dateTo.value;
            if (!App.dates.isPast(to)) to = to > today() ? today() : range.to;
            let from = range.from > to ? to : range.from;
            if (App.dates.span(from, to) > MAX_RANGE_DAYS) from = App.dates.addDays(to, -(MAX_RANGE_DAYS - 1));
            setRange(from, to);
        });

        btnDatePrev.addEventListener("click", () => shiftRange(-1));
        btnDateNext.addEventListener("click", () => shiftRange(1));
        btnDateToday.addEventListener("click", () => setRange(today(), today()));
    }

    function updateDateButtonStates() {
        btnDateNext.disabled = range.to >= today();
        datePicker.max = today();
        dateTo.max = today();
    }

    // ── Fetch Records ────────────────────────────────────────────────────
    // loadedKey is the period the current `records` belong to. Changing the
    // period clears the table first, so one period's rows are never shown
    // under another's dates while the request is in flight.
    let isLoading = false;
    let loadError = false;
    let loadedKey = null;

    function periodQuery() {
        return isRange()
            ? `from=${encodeURIComponent(range.from)}&to=${encodeURIComponent(range.to)}`
            : `date=${encodeURIComponent(range.from)}`;
    }

    function fetchRecords() {
        const requestedKey = rangeKey(range);
        loadError = false;
        if (loadedKey !== requestedKey) {
            records = [];
            isLoading = true;
            updateCountsAndKpis();
            renderTable();
        }

        fetch(`/api/attendance?${periodQuery()}`)
            .then((r) => {
                if (!r.ok) throw new Error("Failed to load records");
                return r.json();
            })
            .then((data) => {
                // The user moved to another period before this answer arrived
                if (requestedKey !== rangeKey(range)) return;
                records = Array.isArray(data) ? data : [];
                loadedKey = requestedKey;
                isLoading = false;
                updateCountsAndKpis();
                renderTable();
                openPendingRecord();
            })
            .catch(() => {
                if (requestedKey !== rangeKey(range)) return;
                records = [];
                loadedKey = null;
                isLoading = false;
                loadError = true;
                updateCountsAndKpis();
                renderTable();
            });
    }

    // ── Counts and KPIs ──────────────────────────────────────────────────
    function updateCountsAndKpis() {
        // Loading, failed or offline: there is no count, and zero would be a false statement
        const offline = isLoading || loadError || App.db.isConnected() === false;
        const kept = records.filter(counted);
        const count = (status) => (offline ? "--" : kept.filter((r) => r.status === status).length);

        const total = offline ? "--" : kept.length;
        const inReview = offline ? "--" : records.length - kept.length;
        const present = count("Present");
        const late = count("Late");
        const unknown = count("Unknown");

        App.setKpi(kpiTotal, total);
        App.setKpi(kpiPresent, present);
        App.setKpi(kpiLate, late);
        App.setKpi(kpiUnknown, unknown);

        updateExportState();

        App.setKpi(countPillAll, total);
        App.setKpi(countPillPresent, present);
        App.setKpi(countPillLate, late);
        App.setKpi(countPillUnknown, unknown);
        App.setKpi(countPillReview, inReview);
    }

    // ── Filtering and Sorting ────────────────────────────────────────────
    function setFilter(filterName) {
        currentFilter = filterName;

        // Sync filter pills
        const pills = filterPillsContainer.querySelectorAll(".filter-pill");
        pills.forEach((p) => {
            if (p.dataset.filter === filterName) {
                p.classList.add("active");
            } else {
                p.classList.remove("active");
            }
        });

        // Sync KPI cards
        kpiCards.forEach((c) => {
            if (c.dataset.filter === filterName) {
                c.classList.add("active");
            } else {
                c.classList.remove("active");
            }
        });

        renderTable();
    }

    // Filter pill click
    filterPillsContainer.addEventListener("click", (e) => {
        const pill = e.target.closest(".filter-pill");
        if (pill) {
            setFilter(pill.dataset.filter);
        }
    });

    // KPI card click
    kpiCards.forEach((card) => {
        card.addEventListener("click", () => {
            setFilter(card.dataset.filter);
        });
    });

    // Sort dropdown change
    sortSelect.addEventListener("change", () => {
        currentSort = sortSelect.value;
        syncHeaderSortIcons();
        renderTable();
    });

    // Table header sort click
    table.querySelectorAll("th.sortable").forEach((th) => {
        th.addEventListener("click", () => {
            const col = th.dataset.col;
            if (sortCol === col) {
                sortAsc = !sortAsc;
            } else {
                sortCol = col;
                sortAsc = true;
            }

            // Sync dropdown if it matches common presets
            if (col === "time_in") {
                currentSort = sortAsc ? "time_asc" : "time_desc";
                sortSelect.value = currentSort;
            } else if (col === "name" && sortAsc) {
                currentSort = "name_asc";
                sortSelect.value = currentSort;
            } else {
                currentSort = "custom";
            }

            syncHeaderSortIcons();
            renderTable();
        });
    });

    function syncHeaderSortIcons() {
        table.querySelectorAll("th.sortable").forEach((th) => {
            th.classList.remove("sorted-asc", "sorted-desc");
            th.removeAttribute("aria-sort");
            const col = th.dataset.col;
            if (currentSort === "time_asc" && col === "time_in") {
                th.classList.add("sorted-asc");
            } else if (currentSort === "time_desc" && col === "time_in") {
                th.classList.add("sorted-desc");
            } else if (currentSort === "name_asc" && col === "name") {
                th.classList.add("sorted-asc");
            } else if (currentSort === "custom" && sortCol === col) {
                th.classList.add(sortAsc ? "sorted-asc" : "sorted-desc");
            }
            if (th.classList.contains("sorted-asc")) th.setAttribute("aria-sort", "ascending");
            if (th.classList.contains("sorted-desc")) th.setAttribute("aria-sort", "descending");
        });
    }

    function getFilteredAndSortedRecords() {
        let filtered;
        if (currentFilter === "Review") {
            filtered = records.filter((r) => !counted(r));
        } else {
            filtered = records.filter(counted);
            if (currentFilter !== "All") filtered = filtered.filter((r) => r.status === currentFilter);
        }

        const sorted = [...filtered];
        const when = (r) => `${r.date || ""} ${r.time_in || ""}`;
        if (currentSort === "time_desc") {
            sorted.sort((a, b) => when(b).localeCompare(when(a)));
        } else if (currentSort === "time_asc") {
            sorted.sort((a, b) => when(a).localeCompare(when(b)));
        } else if (currentSort === "name_asc") {
            sorted.sort((a, b) => (a.name || "").localeCompare(b.name || ""));
        } else if (currentSort === "status_prio") {
            // Late, then Unknown, then Present
            const prio = { Late: 1, Unknown: 2, Present: 3 };
            sorted.sort((a, b) => (prio[a.status] || 9) - (prio[b.status] || 9));
        } else if (currentSort === "custom" && sortCol) {
            sorted.sort((a, b) => {
                let va = a[sortCol] ?? "";
                let vb = b[sortCol] ?? "";
                if (typeof va === "number" && typeof vb === "number") {
                    return sortAsc ? va - vb : vb - va;
                }
                return sortAsc
                    ? String(va).localeCompare(String(vb))
                    : String(vb).localeCompare(String(va));
            });
        }
        return sorted;
    }

    // ── Match quality ────────────────────────────────────────────────────
    // The recognizer reports a distance: lower means closer to the stored
    // images. A word is easier to read than the raw number, so show both.
    // Bands are relative to the current threshold: up to 75% of it is
    // "Close", the rest below it is "Borderline".
    function matchInfo(rec) {
        if (rec.manual || rec.confidence === undefined || rec.confidence === null) {
            return { label: "--", num: "", cls: "", detail: "--" };
        }
        const distance = Number(rec.confidence);
        const num = distance.toFixed(1);
        const limit = App.getThreshold();
        if (rec.status === "Unknown") {
            // 0 means the recognizer had no trained model to compare with
            return distance > 0
                ? { label: "No match", num, cls: "", detail: `No match, distance ${num} (limit ${limit}, lower is closer)` }
                : { label: "No match", num: "", cls: "", detail: "No match" };
        }
        if (distance <= limit * 0.75) {
            return { label: "Close", num, cls: "", detail: `Close match, distance ${num} (limit ${limit}, lower is closer)` };
        }
        return {
            label: "Borderline",
            num,
            cls: "match--borderline",
            detail: `Borderline match, distance ${num} (limit ${limit}, lower is closer)`,
        };
    }

    // An Unknown face has no identity, whatever ID the recognizer guessed
    function personIdText(rec) {
        return rec.status === "Unknown" ? "--" : (rec.person_id ?? "--");
    }

    // ── Table Rendering ──────────────────────────────────────────────────
    function createTableRow(rec) {
        const tr = document.createElement("tr");
        tr.dataset.id = rec.id || "";
        tr.tabIndex = 0;
        tr.setAttribute("data-key-activate", "");

        const statusClass =
            rec.status === "Present" ? "status-badge--present" :
            rec.status === "Late" ? "status-badge--late" :
            "status-badge--unknown";

        const match = matchInfo(rec);
        const matchHtml = match.num
            ? `<span class="${match.cls}">${match.label}</span><span class="match__num">${match.num}</span>`
            : match.label;
        const manualText = rec.manual ? '<span class="manual-tag">Yes</span>' : '<span class="manual-tag">No</span>';

        const dateCell = isRange() ? `<td data-label="Date" class="mono">${App.escapeHtml(rec.date || "--")}</td>` : "";
        tr.innerHTML = `${dateCell}
            <td data-label="Name"><strong>${App.escapeHtml(rec.name || "Unknown")}</strong></td>
            <td data-label="ID" class="col-num"><code>${personIdText(rec)}</code></td>
            <td data-label="Time in" class="mono">${App.escapeHtml(rec.time_in || "--")}</td>
            <td data-label="Status"><span class="status-badge ${statusClass}">${App.escapeHtml(rec.status || "Unknown")}</span>${rec.review && rec.review !== "confirmed" ? ` <span class="review-tag review-tag--${rec.review}">${REVIEW_TEXT[rec.review] || ""}</span>` : ""}</td>
            <td data-label="Emotion">${App.escapeHtml(rec.emotion || "--")}</td>
            <td data-label="Match" title="${App.escapeHtml(match.detail)}"><span>${matchHtml}</span></td>
            <td data-label="Manual">${manualText}</td>
        `;

        tr.addEventListener("click", () => openDrawer(rec));
        return tr;
    }

    function renderTable() {
        const displayRecords = getFilteredAndSortedRecords();
        tableBody.innerHTML = "";
        dateCol.hidden = !isRange();

        if (displayRecords.length === 0) {
            if (isLoading) {
                App.renderState(tableEmpty, "loading", "Loading records...");
            } else if (loadError) {
                App.renderState(tableEmpty, "error", `Could not load records ${periodText()}.`, fetchRecords);
            } else if (App.db.isConnected() === false) {
                App.renderState(tableEmpty, "error", `Database offline. Records ${periodText()} cannot be loaded.`, fetchRecords);
            } else if (currentFilter === "Review") {
                App.renderState(tableEmpty, "empty", `Nothing waiting for review or rejected ${periodText()}.`);
            } else if (currentFilter !== "All" && records.length > 0) {
                App.renderState(tableEmpty, "empty", `No ${currentFilter} records ${periodText()}.`);
            } else {
                App.renderState(tableEmpty, "empty", `No attendance records ${periodText()}.`);
            }
            tableEmpty.style.display = "block";
            table.style.display = "none";
            return;
        }

        tableEmpty.style.display = "none";
        table.style.display = "table";

        displayRecords.forEach((rec) => {
            const tr = createTableRow(rec);
            tableBody.appendChild(tr);
        });

    }

    // ── Detail Drawer ────────────────────────────────────────────────────
    let releaseDrawerFocus = null;
    let drawerRecordId = null;

    // ── Deleting records ─────────────────────────────────────────────────
    // Takes ids out of the local list, whoever deleted them (this page,
    // another browser, or "Delete today's records" in Settings).
    function removeRecords(ids) {
        const gone = new Set(ids || []);
        const before = records.length;
        records = records.filter((r) => !gone.has(r.id));
        if (drawerRecordId && gone.has(drawerRecordId)) closeDrawer();
        if (records.length !== before) {
            updateCountsAndKpis();
            renderTable();
        }
    }

    async function deleteRecord(rec) {
        const confirmed = await App.confirm({
            title: "Delete this record?",
            message:
                `${rec.name || "Unknown"}, ${rec.status || "Unknown"} at ${rec.time_in || "--"} on ${rec.date || "--"}. ` +
                "The record and its face snapshot are removed. This cannot be undone.",
            confirmLabel: "Delete record",
            danger: true,
        });
        if (!confirmed) return;

        try {
            const res = await fetch(`/api/attendance/${encodeURIComponent(rec.id)}`, { method: "DELETE" });
            const data = await res.json().catch(() => ({}));
            if (res.status === 404) {
                // Someone else already removed it: the goal is met
                removeRecords([rec.id]);
                App.showToast("That record was already deleted", "info");
                return;
            }
            if (!res.ok) {
                App.showToast(data.error || "The record could not be deleted", "error");
                return;
            }
            removeRecords([rec.id]);
            App.showToast("Record deleted", "success");
        } catch (err) {
            App.showToast("Could not reach the server. Nothing was deleted.", "error");
        }
    }

    App.socket.on("attendance:deleted", (data) => removeRecords(data && data.ids));

    // A review decision changes whether a record counts
    App.socket.on("review:update", (data) => {
        const rec = data && records.find((r) => r.id === data.id);
        if (!rec) return;
        rec.review = data.review;
        updateCountsAndKpis();
        renderTable();
    });

    // A record another tab asked to show (a click in the Live Monitor feed)
    let pendingRecordId = null;

    function openPendingRecord() {
        if (!pendingRecordId) return;
        const rec = records.find((r) => r.id === pendingRecordId);
        pendingRecordId = null;
        if (rec) {
            openDrawer(rec);
        } else {
            App.showToast("That record is no longer in today's list", "info");
        }
    }

    function openDrawer(rec) {
        const statusClass =
            rec.status === "Present" ? "status-badge--present" :
            rec.status === "Late" ? "status-badge--late" :
            "status-badge--unknown";

        const match = matchInfo(rec);

        let snapshotHtml = "";
        if (rec.has_snapshot && rec.id) {
            snapshotHtml = `
                <div class="drawer-snapshot-box">
                    <img src="/api/attendance/${encodeURIComponent(rec.id)}/snapshot" alt="Face Snapshot" class="drawer-snapshot" onerror="this.parentElement.innerHTML='<div class=\\'drawer-snapshot-placeholder\\'><p>Snapshot load error</p></div>'">
                </div>
            `;
        } else {
            snapshotHtml = `
                <div class="drawer-snapshot-box">
                    <div class="drawer-snapshot-placeholder">
                        <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5" width="40" height="40" class="empty-icon">
                            <rect x="3" y="3" width="18" height="18" rx="2" ry="2"/>
                            <circle cx="8.5" cy="8.5" r="1.5"/>
                            <polyline points="21 15 16 10 5 21"/>
                        </svg>
                        <p>No snapshot available</p>
                    </div>
                </div>
            `;
        }

        drawerBody.innerHTML = `
            ${snapshotHtml}
            <div class="drawer-meta-list">
                <div class="drawer-meta-row">
                    <span class="drawer-meta-label">Name</span>
                    <span class="drawer-meta-val">${App.escapeHtml(rec.name || "Unknown")}</span>
                </div>
                <div class="drawer-meta-row">
                    <span class="drawer-meta-label">Person ID</span>
                    <span class="drawer-meta-val"><code>${personIdText(rec)}</code></span>
                </div>
                <div class="drawer-meta-row">
                    <span class="drawer-meta-label">Status</span>
                    <span class="drawer-meta-val"><span class="status-badge ${statusClass}">${App.escapeHtml(rec.status || "Unknown")}</span></span>
                </div>
                <div class="drawer-meta-row">
                    <span class="drawer-meta-label">Time In</span>
                    <span class="drawer-meta-val">${App.escapeHtml(rec.time_in || "--")}</span>
                </div>
                <div class="drawer-meta-row">
                    <span class="drawer-meta-label">Date</span>
                    <span class="drawer-meta-val">${App.escapeHtml(rec.date || "--")}</span>
                </div>
                <div class="drawer-meta-row">
                    <span class="drawer-meta-label">Emotion</span>
                    <span class="drawer-meta-val">${App.escapeHtml(rec.emotion || "--")}</span>
                </div>
                <div class="drawer-meta-row">
                    <span class="drawer-meta-label">Match</span>
                    <span class="drawer-meta-val">${App.escapeHtml(match.detail)}</span>
                </div>
                ${rec.review ? `<div class="drawer-meta-row">
                    <span class="drawer-meta-label">Review</span>
                    <span class="drawer-meta-val">${REVIEW_TEXT[rec.review] || App.escapeHtml(rec.review)}${rec.review === "pending" ? ". It does not count until confirmed on the Review tab." : ""}</span>
                </div>` : ""}
                <div class="drawer-meta-row">
                    <span class="drawer-meta-label">Type</span>
                    <span class="drawer-meta-val">${rec.manual ? "Marked manually" : "Detected by camera"}</span>
                </div>
                <div class="drawer-meta-row">
                    <span class="drawer-meta-label">Record ID</span>
                    <span class="drawer-meta-val"><code>${App.escapeHtml(rec.id || "--")}</code></span>
                </div>
            </div>
            <div class="drawer-actions">
                <button type="button" class="btn btn--danger-outline" id="btnDeleteRecord">Delete record</button>
            </div>
        `;

        drawerRecordId = rec.id || null;
        const btnDeleteRecord = drawerBody.querySelector("#btnDeleteRecord");
        btnDeleteRecord.disabled = !rec.id;
        btnDeleteRecord.addEventListener("click", () => deleteRecord(rec));

        drawer.inert = false;
        drawer.classList.add("active");
        drawerOverlay.classList.add("active");
        if (!releaseDrawerFocus) releaseDrawerFocus = App.trapFocus(drawer, btnDrawerClose);
    }

    function closeDrawer() {
        drawer.classList.remove("active");
        drawerOverlay.classList.remove("active");
        // inert keeps the off-screen drawer out of the tab order
        drawer.inert = true;
        drawerRecordId = null;
        if (releaseDrawerFocus) {
            releaseDrawerFocus();
            releaseDrawerFocus = null;
        }
    }

    btnDrawerClose.addEventListener("click", closeDrawer);
    drawerOverlay.addEventListener("click", closeDrawer);

    // ── Manual Mark Modal ────────────────────────────────────────────────
    // The person is picked from the registered list, so a typo cannot create
    // a record whose ID and name do not belong together.
    let manualPersons = [];
    let releaseManualFocus = null;

    function showManualError(message) {
        manualError.textContent = message;
        manualError.style.display = message ? "block" : "none";
    }

    async function loadManualPersons() {
        manualPerson.innerHTML = '<option value="">Loading people...</option>';
        manualPerson.disabled = true;
        btnManualSubmit.disabled = true;
        manualPersonHint.hidden = true;

        try {
            const res = await fetch("/api/persons");
            if (!res.ok) throw new Error("Failed to load persons");
            const list = await res.json();
            manualPersons = (Array.isArray(list) ? list : [])
                .sort((a, b) => (a.name || "").localeCompare(b.name || ""));
        } catch (err) {
            manualPersons = [];
            manualPerson.innerHTML = '<option value="">Could not load people</option>';
            showManualError("Could not load the list of registered people.");
            return;
        }

        if (manualPersons.length === 0) {
            manualPerson.innerHTML = '<option value="">No registered people</option>';
            manualPersonHint.hidden = false;
            return;
        }

        manualPerson.innerHTML = "";
        const placeholder = new Option("Select a person", "", true, true);
        placeholder.disabled = true;
        manualPerson.appendChild(placeholder);
        manualPersons.forEach((p) => {
            manualPerson.appendChild(new Option(`${p.name} (ID ${p.person_id})`, String(p.person_id)));
        });
        manualPerson.disabled = false;
        btnManualSubmit.disabled = false;
        manualPerson.focus();
    }

    function openManualModal() {
        showManualError("");
        manualForm.reset();
        manualModalOverlay.classList.add("active");
        releaseManualFocus = App.trapFocus(manualModalOverlay.querySelector(".modal"), manualModalClose);
        loadManualPersons();
    }

    function closeManualModal() {
        manualModalOverlay.classList.remove("active");
        if (releaseManualFocus) {
            releaseManualFocus();
            releaseManualFocus = null;
        }
    }

    btnManualMark.addEventListener("click", openManualModal);
    manualModalClose.addEventListener("click", closeManualModal);
    manualModalCancel.addEventListener("click", closeManualModal);
    manualModalOverlay.addEventListener("click", (e) => {
        if (e.target === manualModalOverlay) closeManualModal();
    });

    manualForm.addEventListener("submit", (e) => {
        e.preventDefault();
        showManualError("");

        const person = manualPersons.find((p) => String(p.person_id) === manualPerson.value);
        if (!person) {
            showManualError("Select a person.");
            return;
        }
        const statusVal = manualStatus.value;

        btnManualSubmit.disabled = true;
        fetch("/api/attendance/manual", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({
                person_id: person.person_id,
                name: person.name,
                status: statusVal,
            }),
        })
            .then(async (r) => {
                const data = await r.json();
                if (!r.ok) {
                    throw new Error(data.error || "Failed to mark attendance");
                }
                return data;
            })
            .then((data) => {
                App.showToast(`${person.name} marked ${statusVal}`, "success");
                closeManualModal();

                // If the shown period includes today, add it to the table
                if (inRange(today()) && data.record) {
                    const exists = records.some((r) => r.id === data.record.id);
                    if (!exists) {
                        records.unshift(data.record);
                        updateCountsAndKpis();
                        renderTable();
                    }
                }
            })
            .catch((err) => {
                showManualError(err.message);
            })
            .finally(() => {
                btnManualSubmit.disabled = false;
            });
    });

    // ── Export Actions ───────────────────────────────────────────────────
    function startExport(format, label) {
        const period = isRange() ? `${range.from} to ${range.to}` : range.from;
        App.showToast(`Downloading ${label} for ${period} (${records.length} records)`, "info");
        App.download(`/api/export/${format}?${periodQuery()}`);
    }

    btnExportExcel.addEventListener("click", () => startExport("excel", "Excel file"));
    btnExportCsv.addEventListener("click", () => startExport("csv", "CSV file"));

    function updateExportState() {
        const nothing = records.length === 0;
        [btnExportExcel, btnExportCsv].forEach((btn) => {
            btn.disabled = nothing;
            btn.title = nothing ? `No records to export ${periodText()}` : "";
        });
    }

    // ── Global Keydown (Esc to close drawer/modal) ────────────────────────
    window.addEventListener("keydown", (e) => {
        if (e.key === "Escape") {
            if (manualModalOverlay.classList.contains("active")) {
                closeManualModal();
            } else if (drawer.classList.contains("active")) {
                closeDrawer();
            }
        }
    });

    // ── Live WebSocket Updates ───────────────────────────────────────────
    App.socket.on("attendance:new", (record) => {
        if (record && record.id && inRange(record.date || today())) {
            const exists = records.some((r) => r.id === record.id);
            if (!exists) {
                records.unshift(record);
                updateCountsAndKpis();
                const show = counted(record)
                    ? currentFilter === "All" || currentFilter === record.status
                    : currentFilter === "Review";
                if (show) {
                    tableEmpty.style.display = "none";
                    table.style.display = "table";
                    const tr = createTableRow(record);
                    tr.classList.add("is-new");
                    tableBody.prepend(tr);
                }
            }
        }
    });

    // ── Cross-Navigation Support (tab:navigate) ──────────────────────────
    window.addEventListener("tab:navigate", (e) => {
        if (e.detail && e.detail.tab === "attendance") {
            // A tab click wrote "#attendance": put the shown period back.
            // When the URL itself caused the switch, route:change handles it.
            if (!e.detail.opts || !e.detail.opts.fromRoute) syncRoute();
            if (e.detail.opts && e.detail.opts.filter) {
                setFilter(e.detail.opts.filter);
            }
            if (e.detail.opts && e.detail.opts.recordId) {
                // Feed items are always today's records
                pendingRecordId = e.detail.opts.recordId;
                if (currentFilter !== "All") setFilter("All");
                if (range.from !== today() || range.to !== today()) {
                    setRange(today(), today());
                } else if (records.some((r) => r.id === pendingRecordId)) {
                    openPendingRecord();
                } else {
                    fetchRecords();
                }
            }
        }
    });

    // Back/forward or a hand-edited URL changed the period
    window.addEventListener("route:change", (e) => {
        if (e.detail.tab !== "attendance") return;
        const wanted = rangeFromArg(e.detail.arg);
        if (rangeKey(wanted) !== rangeKey(range)) {
            range = wanted;
            datePicker.value = range.from;
            dateTo.value = range.to;
            updateDateButtonStates();
            fetchRecords();
        }
        // Rewrites an invalid or future period in the URL to what is shown
        syncRoute();
    });

    // Database went away or came back: reload so the table tells the truth
    window.addEventListener("db:status", fetchRecords);

    // Match labels depend on the threshold
    window.addEventListener("threshold:change", renderTable);

    // ── Initialize ───────────────────────────────────────────────────────
    initDateControls();
    syncHeaderSortIcons();
    fetchRecords();
})();
