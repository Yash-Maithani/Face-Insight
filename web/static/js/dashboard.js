/* ==========================================================================
   dashboard.js - Dashboard analytics tab
   Chart.js charts (attendance trend bars, emotion doughnut, days attended),
   KPI cards (Top Emotion, Week Total, Today Total, Late Rate),
   cross-navigation to Attendance tab, and debounced live refresh.
   ========================================================================== */

(function () {
    "use strict";

    // ── Chart colors come from the CSS tokens of the active theme ────────
    function cssVar(name) {
        return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
    }

    function applyChartDefaults() {
        if (!window.Chart) return;
        Chart.defaults.color = cssVar("--text-dim");
        Chart.defaults.borderColor = cssVar("--border");
        Chart.defaults.font.family = cssVar("--font-sans");
    }

    applyChartDefaults();

    // Last fetched chart data, kept so a theme change can redraw without refetching
    let lastCharts = null;

    // ── Chart Instances ──────────────────────────────────────────────────
    let chartWeekly = null;
    let chartEmotions = null;
    let chartPersons = null;

    // ── DOM Elements ─────────────────────────────────────────────────────
    const kpiTopEmotion = document.getElementById("kpiTopEmotion");
    const kpiWeekTotal = document.getElementById("kpiWeekTotal");
    const kpiTodayTotal = document.getElementById("kpiTodayTotal");
    const kpiLateRate = document.getElementById("kpiLateRate");
    const kpiCardLateRate = document.getElementById("kpiCardLateRate");

    const canvasWeekly = document.getElementById("chartWeekly");
    const emptyWeekly = document.getElementById("chartWeeklyEmpty");

    const canvasEmotions = document.getElementById("chartEmotions");
    const emptyEmotions = document.getElementById("chartEmotionsEmpty");

    const canvasPersons = document.getElementById("chartPersons");
    const emptyPersons = document.getElementById("chartPersonsEmpty");

    // ── Clickable Late Rate KPI ──────────────────────────────────────────
    if (kpiCardLateRate) {
        kpiCardLateRate.addEventListener("click", () => {
            App.navigateTo("attendance", { filter: "Late" });
        });
    }

    // ── Period (7, 14 or 30 days) ────────────────────────────────────────
    // Kept in the URL as #dashboard/14 so a reload shows the same view.
    const RANGES = [7, 14, 30];
    const rangeButtons = document.querySelectorAll("#rangeSwitch [data-range]");
    const kpiRangeLabel = document.getElementById("kpiRangeLabel");
    const chartWeeklyTitle = document.getElementById("chartWeeklyTitle");
    const chartPersonsTitle = document.getElementById("chartPersonsTitle");

    function rangeFromRoute(route) {
        const n = parseInt(route.arg, 10);
        return route.tab === "dashboard" && RANGES.includes(n) ? n : 7;
    }

    let rangeDays = rangeFromRoute(App.route.get());

    function syncRoute() {
        App.route.setArg("dashboard", rangeDays === 7 ? "" : String(rangeDays));
    }

    function renderRange() {
        rangeButtons.forEach((btn) => {
            btn.setAttribute("aria-pressed", String(Number(btn.dataset.range) === rangeDays));
        });
        kpiRangeLabel.textContent = `Records, last ${rangeDays} days`;
        chartWeeklyTitle.textContent = `Attendance Trend, last ${rangeDays} days`;
        chartPersonsTitle.textContent = `Days Attended, last ${rangeDays} days`;
    }

    function setRange(days) {
        if (days === rangeDays) return;
        rangeDays = days;
        renderRange();
        syncRoute();
        refreshDashboard();
    }

    rangeButtons.forEach((btn) => {
        btn.addEventListener("click", () => setRange(Number(btn.dataset.range)));
    });

    renderRange();

    // ── Fetch and Render Dashboard ───────────────────────────────────────
    let isFetching = false;
    let refetchWanted = false;
    let loadFailed = false;

    // Returns the parsed JSON, or the fallback and marks the load as failed
    function getJson(url, fallback) {
        return fetch(url)
            .then((r) => {
                if (!r.ok) throw new Error(url + " " + r.status);
                return r.json();
            })
            .catch(() => {
                loadFailed = true;
                return fallback;
            });
    }

    function refreshDashboard() {
        if (isFetching) {
            // A request is in flight (maybe for another period): run again after it
            refetchWanted = true;
            return;
        }
        isFetching = true;
        loadFailed = false;

        Promise.all([
            getJson(`/api/stats/weekly?days=${rangeDays}`, []),
            getJson("/api/stats/emotions", {}),
            getJson(`/api/stats/persons?days=${rangeDays}`, []),
            getJson("/api/stats/today", {}),
        ])
            .then(([weekly, emotions, persons, todayStats]) => {
                renderKpis(weekly, emotions, todayStats);
                lastCharts = { weekly, emotions, persons };
                renderCharts();
            })
            .finally(() => {
                isFetching = false;
                if (refetchWanted) {
                    refetchWanted = false;
                    refreshDashboard();
                }
            });
    }

    function renderCharts() {
        if (!lastCharts) return;
        renderWeeklyChart(lastCharts.weekly);
        renderEmotionsChart(lastCharts.emotions);
        renderPersonsChart(lastCharts.persons);
    }

    window.addEventListener("themechange", () => {
        applyChartDefaults();
        renderCharts();
    });

    // Empty states say why there is nothing to draw
    function showEmpty(canvas, emptyEl, message) {
        if (loadFailed) {
            App.renderState(emptyEl, "error", "Could not load chart data.", refreshDashboard);
        } else if (App.db.isConnected() === false) {
            App.renderState(emptyEl, "error", "Database offline. Chart data cannot be loaded.", refreshDashboard);
        } else {
            App.renderState(emptyEl, "empty", message);
        }
        canvas.style.display = "none";
        emptyEl.style.display = "flex";
    }

    // ── Render KPIs ──────────────────────────────────────────────────────
    function renderKpis(weekly, emotions, todayStats) {
        // Offline or a failed load: every figure here comes from the database, so show none
        if (loadFailed || App.db.isConnected() === false) {
            [kpiTopEmotion, kpiWeekTotal, kpiTodayTotal, kpiLateRate].forEach((el) => App.setKpi(el, "--"));
            return;
        }

        // 1. Top Emotion Today
        let topEmotion = "--";
        let maxCount = 0;
        if (emotions && typeof emotions === "object") {
            for (const [emo, cnt] of Object.entries(emotions)) {
                if (cnt > maxCount && emo && emo !== "--") {
                    maxCount = cnt;
                    topEmotion = emo;
                }
            }
        }
        App.setKpi(kpiTopEmotion, topEmotion);

        // 2. Records in the selected period
        let weekTotal = 0;
        if (Array.isArray(weekly)) {
            weekly.forEach((d) => {
                weekTotal += (d.present_count || 0) + (d.late_count || 0) + (d.unknown_count || 0);
            });
        }
        App.setKpi(kpiWeekTotal, weekTotal);

        // 3. Today Total
        const todayTotal = todayStats.total !== undefined ? todayStats.total : "--";
        App.setKpi(kpiTodayTotal, todayTotal);

        // 4. Late Rate = today's Late / (today's Present + Late)
        const pres = todayStats.present || 0;
        const late = todayStats.late || 0;
        const knownTotal = pres + late;

        if (knownTotal > 0) {
            const rate = ((late / knownTotal) * 100).toFixed(1) + "%";
            App.setKpi(kpiLateRate, rate);
        } else {
            App.setKpi(kpiLateRate, "--");
        }
    }

    // ── Render Weekly Stacked Bar Chart ──────────────────────────────────
    function renderWeeklyChart(data) {
        if (!canvasWeekly) return;

        if (chartWeekly) {
            chartWeekly.destroy();
            chartWeekly = null;
        }

        const hasData = Array.isArray(data) && data.some((d) =>
            (d.present_count || 0) + (d.late_count || 0) + (d.unknown_count || 0) > 0
        );

        if (!hasData) {
            showEmpty(canvasWeekly, emptyWeekly, `No attendance records in the last ${rangeDays} days`);
            return;
        }

        canvasWeekly.style.display = "block";
        emptyWeekly.style.display = "none";

        const labels = data.map((d) => {
            // Format YYYY-MM-DD to MM/DD
            const parts = (d.date || "").split("-");
            return parts.length === 3 ? `${parts[1]}/${parts[2]}` : d.date;
        });

        const presentData = data.map((d) => d.present_count || 0);
        const lateData = data.map((d) => d.late_count || 0);
        const unknownData = data.map((d) => d.unknown_count || 0);

        chartWeekly = new Chart(canvasWeekly, {
            type: "bar",
            data: {
                labels: labels,
                datasets: [
                    {
                        label: "Present",
                        data: presentData,
                        backgroundColor: cssVar("--success"),
                        borderRadius: 4,
                    },
                    {
                        label: "Late",
                        data: lateData,
                        backgroundColor: cssVar("--warning"),
                        borderRadius: 4,
                    },
                    {
                        label: "Unknown",
                        data: unknownData,
                        backgroundColor: cssVar("--danger"),
                        borderRadius: 4,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: "top",
                        labels: { boxWidth: 12, padding: 16 },
                    },
                    tooltip: {
                        mode: "index",
                        intersect: false,
                    },
                },
                scales: {
                    x: {
                        stacked: false,
                        grid: { display: false },
                    },
                    y: {
                        stacked: false,
                        beginAtZero: true,
                        ticks: { stepSize: 1, precision: 0 },
                        grid: { color: cssVar("--border") },
                    },
                },
            },
        });
    }

    // ── Render Emotions Doughnut Chart ───────────────────────────────────
    function renderEmotionsChart(data) {
        if (!canvasEmotions) return;

        if (chartEmotions) {
            chartEmotions.destroy();
            chartEmotions = null;
        }

        const entries = Object.entries(data || {}).filter(([emo, count]) => count > 0 && emo && emo !== "--");
        if (entries.length === 0) {
            showEmpty(canvasEmotions, emptyEmotions, "No emotion detections recorded today");
            return;
        }

        canvasEmotions.style.display = "block";
        emptyEmotions.style.display = "none";

        const labels = entries.map(([emo]) => emo);
        const counts = entries.map(([_, count]) => count);

        const palette = [1, 2, 3, 4, 5, 6, 7].map((n) => cssVar("--chart-" + n));

        chartEmotions = new Chart(canvasEmotions, {
            type: "doughnut",
            data: {
                labels: labels,
                datasets: [
                    {
                        data: counts,
                        backgroundColor: labels.map((_, i) => palette[i % palette.length]),
                        borderWidth: 2,
                        borderColor: cssVar("--surface"),
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                plugins: {
                    legend: {
                        position: "right",
                        labels: { boxWidth: 12, padding: 12 },
                    },
                },
                cutout: "65%",
            },
        });
    }

    // ── Render Persons Horizontal Bar Chart ──────────────────────────────
    // One bar per registered person: the number of days in the period with
    // at least one record. Unknown faces are not people, so they are left out.
    function renderPersonsChart(data) {
        if (!canvasPersons) return;

        if (chartPersons) {
            chartPersons.destroy();
            chartPersons = null;
        }

        const people = (Array.isArray(data) ? data : [])
            .filter((p) => p.name && p.name !== "Unknown")
            .map((p) => ({ label: p.name, days: p.days !== undefined ? p.days : p.count || 0 }))
            .sort((a, b) => b.days - a.days)
            .slice(0, 10);

        if (people.length === 0) {
            showEmpty(canvasPersons, emptyPersons, `No registered person was recorded in the last ${rangeDays} days`);
            return;
        }

        canvasPersons.style.display = "block";
        emptyPersons.style.display = "none";

        chartPersons = new Chart(canvasPersons, {
            type: "bar",
            data: {
                labels: people.map((p) => p.label),
                datasets: [
                    {
                        label: "Days attended",
                        data: people.map((p) => p.days),
                        backgroundColor: cssVar("--accent"),
                        borderRadius: 4,
                    },
                ],
            },
            options: {
                responsive: true,
                maintainAspectRatio: false,
                indexAxis: "y", // Horizontal bars
                plugins: {
                    legend: { display: false },
                },
                scales: {
                    x: {
                        beginAtZero: true,
                        max: rangeDays,
                        ticks: { stepSize: 1, precision: 0 },
                        grid: { color: cssVar("--border") },
                    },
                    y: {
                        grid: { display: false },
                    },
                },
            },
        });
    }

    // ── Debounced Refresh on attendance:new ───────────────────────────────
    let debounceTimer = null;
    function refreshSoon() {
        if (debounceTimer) clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => {
            refreshDashboard();
        }, 600);
    }

    App.socket.on("attendance:new", refreshSoon);
    App.socket.on("attendance:deleted", refreshSoon);

    // Database went away or came back: redraw so the page tells the truth
    window.addEventListener("db:status", refreshDashboard);

    // ── Tab Navigate Event ───────────────────────────────────────────────
    window.addEventListener("tab:navigate", (e) => {
        if (e.detail && e.detail.tab === "dashboard") {
            // A tab click wrote "#dashboard": put the selected period back
            if (!e.detail.opts || !e.detail.opts.fromRoute) syncRoute();
            refreshDashboard();
        }
    });

    // Back/forward or a hand-edited URL changed the period
    window.addEventListener("route:change", (e) => {
        if (e.detail.tab !== "dashboard") return;
        const wanted = rangeFromRoute(e.detail);
        if (wanted !== rangeDays) {
            rangeDays = wanted;
            renderRange();
            refreshDashboard();
        }
        syncRoute();
    });

    // Until the first answer arrives, say so instead of showing blank cards
    [[canvasWeekly, emptyWeekly], [canvasEmotions, emptyEmotions], [canvasPersons, emptyPersons]].forEach(
        ([canvas, emptyEl]) => {
            App.renderState(emptyEl, "loading", "Loading...");
            canvas.style.display = "none";
            emptyEl.style.display = "flex";
        }
    );

    // Initial load
    refreshDashboard();
})();
