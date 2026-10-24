/* ==========================================================================
   reports.js - Reports tab
   Pick dates and people, see the report, download it as CSV or PDF. The
   table shows the same rows the files contain.
   ========================================================================== */

(function () {
    "use strict";

    const MAX_RANGE_DAYS = 366;
    const PREVIEW_ROWS = 500; // the files always have every row

    const fromInput = document.getElementById("reportFrom");
    const toInput = document.getElementById("reportTo");
    const errRange = document.getElementById("errReportRange");
    const everyone = document.getElementById("reportEveryone");
    const peopleList = document.getElementById("reportPeopleList");
    const btnCsv = document.getElementById("btnReportCsv");
    const btnPdf = document.getElementById("btnReportPdf");
    const summaryNote = document.getElementById("reportSummaryNote");
    const totalsTable = document.getElementById("reportTotalsTable");
    const totalsBody = document.getElementById("reportTotalsBody");
    const emptyEl = document.getElementById("reportEmpty");
    const rowsCard = document.getElementById("reportRowsCard");
    const rowsBody = document.getElementById("reportRowsBody");

    const STATUS_CLASS = {
        Present: "status-badge--present",
        Late: "status-badge--late",
        Absent: "status-badge--absent",
        "Not seen yet": "status-badge--pending",
    };

    let seq = 0;
    let peopleLoaded = false;
    let lastQuery = null;

    function firstOfMonth() {
        return App.dates.today().slice(0, 8) + "01";
    }

    fromInput.value = firstOfMonth();
    toInput.value = App.dates.today();

    function setDownloads(enabled) {
        [btnCsv, btnPdf].forEach((b) => {
            b.disabled = !enabled;
            b.title = enabled ? "" : "Nothing to download for these dates and people";
        });
    }

    function chosenIds() {
        if (everyone.checked) return null;
        return Array.from(peopleList.querySelectorAll("input:checked")).map((b) => b.value);
    }

    function query() {
        const params = new URLSearchParams({ from: fromInput.value, to: toInput.value });
        const ids = chosenIds();
        if (ids) params.set("people", ids.join(","));
        return params.toString();
    }

    function showMessage(kind, text, retry) {
        totalsTable.hidden = true;
        rowsCard.hidden = true;
        summaryNote.textContent = "";
        App.renderState(emptyEl, kind, text, retry);
        setDownloads(false);
    }

    async function loadPeople() {
        try {
            const res = await fetch("/api/people");
            if (!res.ok) throw new Error("people");
            const data = await res.json();
            const keep = new Set(chosenIds() || []);
            peopleList.innerHTML = (data.people || [])
                .map((p) => `<label class="check-chip"><input type="checkbox" value="${p.person_id}" ${keep.has(String(p.person_id)) ? "checked" : ""}><span>${App.escapeHtml(p.name)}</span></label>`)
                .join("");
            peopleLoaded = true;
        } catch (err) {
            peopleList.innerHTML = "";
        }
    }

    async function load() {
        errRange.textContent = "";
        const from = fromInput.value;
        const to = toInput.value;
        if (!App.dates.isPast(from) || !App.dates.isPast(to)) {
            errRange.textContent = "Pick two dates, neither after today.";
            showMessage("empty", "Choose the dates to report on.");
            return;
        }
        if (from > to) {
            errRange.textContent = "The start date must be on or before the end date.";
            showMessage("empty", "Choose the dates to report on.");
            return;
        }
        if (App.dates.span(from, to) > MAX_RANGE_DAYS) {
            errRange.textContent = `A report can cover at most ${MAX_RANGE_DAYS} days.`;
            showMessage("empty", "Choose a shorter range.");
            return;
        }
        const ids = chosenIds();
        if (ids && !ids.length) {
            showMessage("empty", "Pick at least one person, or tick Everyone.");
            return;
        }

        const mine = ++seq;
        const q = query();
        showMessage("loading", "Building the report...");
        let res;
        let data = {};
        try {
            res = await fetch(`/api/reports?${q}`);
            data = await res.json().catch(() => ({}));
        } catch (err) {
            res = null;
        }
        if (mine !== seq) return;
        if (!res || !res.ok) {
            const text = res && res.status === 503
                ? "Database offline. Reports need the database."
                : (data.error || "Could not build the report.");
            showMessage("error", text, load);
            return;
        }
        lastQuery = q;
        render(data);
    }

    function render(report) {
        if (!report.people.length) {
            showMessage("empty", "No one is registered yet, so there is nothing to report.");
            return;
        }
        if (!report.rows.length) {
            showMessage("empty", "Nothing to report: no working days and no records in these dates.");
            return;
        }
        summaryNote.textContent = `${report.from} to ${report.to}, ${report.people.length} ${report.people.length === 1 ? "person" : "people"}, ${report.rows.length} rows`;
        totalsBody.innerHTML = report.totals
            .map((t) => `
                <tr>
                    <td data-label="Name"><strong>${App.escapeHtml(t.name)}</strong></td>
                    <td data-label="ID" class="col-num"><code>${t.person_id}</code></td>
                    <td data-label="Present" class="col-num">${t.Present}</td>
                    <td data-label="Late" class="col-num">${t.Late}</td>
                    <td data-label="Absent" class="col-num">${t.Absent}</td>
                    <td data-label="Not seen yet" class="col-num">${t["Not seen yet"]}</td>
                    <td data-label="Left early" class="col-num">${t["Left early"]}</td>
                </tr>`)
            .join("");
        totalsTable.hidden = false;
        emptyEl.innerHTML = "";

        const shown = report.rows.slice(0, PREVIEW_ROWS);
        rowsBody.innerHTML = shown
            .map((r) => `
                <tr>
                    <td data-label="Date" class="mono">${r.date}</td>
                    <td data-label="ID" class="col-num"><code>${r.person_id}</code></td>
                    <td data-label="Name">${App.escapeHtml(r.name)}</td>
                    <td data-label="Status"><span class="status-badge ${STATUS_CLASS[r.status] || ""}">${App.escapeHtml(r.status)}</span></td>
                    <td data-label="Left early">${r.left_early ? "Yes" : "No"}</td>
                    <td data-label="First seen" class="mono">${r.first_seen || "--"}</td>
                    <td data-label="Last seen" class="mono">${r.last_seen || "--"}</td>
                    <td data-label="Records" class="col-num">${r.sightings}</td>
                    <td data-label="Manual">${r.manual ? "Yes" : "No"}</td>
                </tr>`)
            .join("") + (report.rows.length > shown.length
            ? `<tr><td colspan="9" class="table-note">Showing the first ${PREVIEW_ROWS} of ${report.rows.length} rows. The CSV and PDF have all of them.</td></tr>`
            : "");
        rowsCard.hidden = false;
        setDownloads(true);
    }

    function download(fmt) {
        if (!lastQuery) return;
        App.download(`/api/reports/export.${fmt}?${lastQuery}`);
        App.showToast(`Downloading the ${fmt.toUpperCase()} report`, "info");
    }

    btnCsv.addEventListener("click", () => download("csv"));
    btnPdf.addEventListener("click", () => download("pdf"));

    [fromInput, toInput].forEach((el) => el.addEventListener("change", load));

    everyone.addEventListener("change", () => {
        if (everyone.checked) peopleList.querySelectorAll("input").forEach((b) => (b.checked = false));
        load();
    });

    peopleList.addEventListener("change", () => {
        everyone.checked = peopleList.querySelectorAll("input:checked").length === 0;
        load();
    });

    window.addEventListener("tab:navigate", async (e) => {
        if (!e.detail || e.detail.tab !== "reports") return;
        fromInput.max = App.dates.today();
        toInput.max = App.dates.today();
        await loadPeople();
        load();
    });

    window.addEventListener("db:status", () => {
        if (document.getElementById("panel-reports").classList.contains("active") && peopleLoaded) load();
    });

    setDownloads(false);
})();
