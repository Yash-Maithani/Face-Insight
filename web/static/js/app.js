/* ==========================================================================
   app.js - Main application script
   Socket.IO singleton, theme, tab navigation, shared helpers, Live Monitor logic.
   ========================================================================== */

(function () {
    "use strict";

    // ── Socket.IO singleton ──────────────────────────────────────────────
    const socket = io({ transports: ["websocket", "polling"] });

    // ── Toast helper ─────────────────────────────────────────────────────
    let toastContainer = document.querySelector(".toast-container");
    if (!toastContainer) {
        toastContainer = document.createElement("div");
        toastContainer.className = "toast-container";
        toastContainer.setAttribute("role", "status");
        toastContainer.setAttribute("aria-live", "polite");
        document.body.appendChild(toastContainer);
    }

    function showToast(message, type = "info", duration) {
        // Errors stay up longer: they usually need reading twice
        if (duration === undefined) duration = type === "error" ? 6000 : 3500;
        const el = document.createElement("div");
        el.className = `toast toast--${type}`;
        if (type === "error") el.setAttribute("role", "alert");
        el.textContent = message;
        toastContainer.appendChild(el);
        setTimeout(() => {
            el.style.opacity = "0";
            el.style.transform = "translateY(12px)";
            el.style.transition = "all 0.2s ease-in";
            setTimeout(() => el.remove(), 200);
        }, duration);
    }

    // ── Dialog focus handling ────────────────────────────────────────────
    // Moves focus into a dialog, keeps Tab inside it, and hands focus back to
    // whatever opened it. Returns a function that releases the trap.
    const FOCUSABLE =
        'a[href], button:not([disabled]), input:not([disabled]), select:not([disabled]), ' +
        'textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

    const trapStack = [];

    function trapFocus(container, initialFocus) {
        const opener = document.activeElement;
        const token = {};
        trapStack.push(token);
        const items = () =>
            [...container.querySelectorAll(FOCUSABLE)].filter((el) => !el.hidden && el.getClientRects().length > 0);

        function onKeydown(e) {
            if (e.key !== "Tab") return;
            // A dialog opened on top of this one owns the keyboard for now
            if (trapStack[trapStack.length - 1] !== token) return;
            const list = items();
            if (list.length === 0) {
                e.preventDefault();
                container.focus();
                return;
            }
            const first = list[0];
            const last = list[list.length - 1];
            const current = document.activeElement;
            if (!container.contains(current)) {
                e.preventDefault();
                first.focus();
            } else if (e.shiftKey && (current === first || current === container)) {
                e.preventDefault();
                last.focus();
            } else if (!e.shiftKey && current === last) {
                e.preventDefault();
                first.focus();
            }
        }

        document.addEventListener("keydown", onKeydown, true);
        (initialFocus || items()[0] || container).focus();

        return function release() {
            document.removeEventListener("keydown", onKeydown, true);
            const index = trapStack.indexOf(token);
            if (index !== -1) trapStack.splice(index, 1);
            if (opener && document.contains(opener) && typeof opener.focus === "function") opener.focus();
        };
    }

    // ── Confirm dialog ───────────────────────────────────────────────────
    // In-app replacement for window.confirm(). Resolves true or false.
    // `extra` ({label, onClick}) adds a third button that runs without closing
    // the dialog, for example "Export first" before a delete.
    function confirmDialog({ title, message, confirmLabel = "Confirm", danger = false, extra = null }) {
        return new Promise((resolve) => {
            const overlay = document.createElement("div");
            overlay.className = "modal-overlay active";
            overlay.innerHTML = `
                <div class="modal" role="alertdialog" aria-modal="true" aria-labelledby="confirmTitle" aria-describedby="confirmMessage" tabindex="-1">
                    <div class="modal__header">
                        <h3 class="modal__title" id="confirmTitle"></h3>
                    </div>
                    <div class="modal__body">
                        <p class="confirm-message" id="confirmMessage"></p>
                        <div class="modal__footer">
                            <button type="button" class="btn btn--ghost confirm-extra" data-action="extra" hidden></button>
                            <button type="button" class="btn btn--ghost" data-action="cancel">Cancel</button>
                            <button type="button" class="btn" data-action="confirm"></button>
                        </div>
                    </div>
                </div>
            `;
            overlay.querySelector("#confirmTitle").textContent = title;
            overlay.querySelector("#confirmMessage").textContent = message;
            const confirmBtn = overlay.querySelector('[data-action="confirm"]');
            confirmBtn.textContent = confirmLabel;
            confirmBtn.classList.add(danger ? "btn--danger" : "btn--primary");
            if (extra) {
                const extraBtn = overlay.querySelector('[data-action="extra"]');
                extraBtn.textContent = extra.label;
                extraBtn.hidden = false;
            }
            document.body.appendChild(overlay);

            // Focus starts on Cancel so Enter never confirms by accident
            const release = trapFocus(overlay.querySelector(".modal"), overlay.querySelector('[data-action="cancel"]'));

            function close(result) {
                document.removeEventListener("keydown", onKeydown, true);
                overlay.remove();
                release();
                resolve(result);
            }

            function onKeydown(e) {
                if (e.key === "Escape") {
                    e.stopPropagation();
                    close(false);
                }
            }

            document.addEventListener("keydown", onKeydown, true);
            overlay.addEventListener("click", (e) => {
                const action = e.target.closest("[data-action]");
                if (action && action.dataset.action === "extra") {
                    extra.onClick();
                } else if (action) {
                    close(action.dataset.action === "confirm");
                } else if (e.target === overlay) {
                    close(false);
                }
            });
        });
    }

    // ── Download ─────────────────────────────────────────────────────────
    // Starts a file download without navigating away, so a page with unsaved
    // settings does not ask "leave site?".
    function download(url) {
        const link = document.createElement("a");
        link.href = url;
        link.download = "";
        link.hidden = true;
        document.body.appendChild(link);
        link.click();
        link.remove();
    }

    // ── Keyboard activation ──────────────────────────────────────────────
    // Elements marked data-key-activate (table rows, sortable headers, KPI
    // cards) respond to Enter and Space the way a button does.
    document.addEventListener("keydown", (e) => {
        if (e.key !== "Enter" && e.key !== " ") return;
        const el = e.target;
        if (!(el instanceof HTMLElement) || !el.hasAttribute("data-key-activate")) return;
        e.preventDefault();
        el.click();
    });

    // ── setKpi helper ────────────────────────────────────────────────────
    function setKpi(el, value) {
        if (!el) return;

        if (value === null || value === undefined) {
            value = "--";
        }

        const valueStr = String(value).trim();
        const prefersReducedMotion = window.matchMedia &&
            window.matchMedia("(prefers-reduced-motion: reduce)").matches;

        if (el._kpiRaf) {
            cancelAnimationFrame(el._kpiRaf);
            el._kpiRaf = null;
        }

        if (prefersReducedMotion || valueStr === "--") {
            el.textContent = valueStr;
            el._kpiCurrent = valueStr;
            return;
        }

        // Match integer, decimal, or percentage: e.g. "42", "12.5", "33.3%", "100%"
        const match = valueStr.match(/^([+-]?\d+(?:\.\d+)?)(%)?$/);
        if (!match) {
            el.textContent = valueStr;
            el._kpiCurrent = valueStr;
            return;
        }

        const targetNum = parseFloat(match[1]);
        const isPercent = match[2] === "%";
        const decimalPlaces = match[1].includes(".") ? match[1].split(".")[1].length : 0;

        let startNum = 0;
        const prevStr = el._kpiCurrent !== undefined ? String(el._kpiCurrent) : el.textContent.trim();
        const prevMatch = prevStr.match(/^([+-]?\d+(?:\.\d+)?)(%)?$/);
        if (prevMatch) {
            startNum = parseFloat(prevMatch[1]);
        } else {
            startNum = 0;
        }

        if (startNum === targetNum) {
            el.textContent = valueStr;
            el._kpiCurrent = valueStr;
            return;
        }

        const duration = 500; // ms
        const startTime = performance.now();

        function step(now) {
            const elapsed = now - startTime;
            const progress = Math.min(elapsed / duration, 1);
            // Cubic ease-out: 1 - (1 - t)^3
            const easeOut = 1 - Math.pow(1 - progress, 3);
            const currentNum = startNum + (targetNum - startNum) * easeOut;

            let displayStr = decimalPlaces > 0
                ? currentNum.toFixed(decimalPlaces)
                : Math.round(currentNum).toString();

            if (isPercent) displayStr += "%";
            el.textContent = displayStr;

            if (progress < 1) {
                el._kpiRaf = requestAnimationFrame(step);
            } else {
                el.textContent = valueStr;
                el._kpiCurrent = valueStr;
                el._kpiRaf = null;
            }
        }

        el._kpiRaf = requestAnimationFrame(step);
    }

    // ── staggerIn helper ─────────────────────────────────────────────────
    function staggerIn(elements, baseDelay = 30, maxItems = 10) {
        if (!elements) return;
        const list = Array.isArray(elements) ? elements : Array.from(elements);
        const prefersReducedMotion = window.matchMedia &&
            window.matchMedia("(prefers-reduced-motion: reduce)").matches;

        list.forEach((el, index) => {
            if (!el) return;
            if (prefersReducedMotion) {
                el.style.animation = "none";
                return;
            }
            const delay = Math.min(index, maxItems - 1) * baseDelay;
            el.style.animation = `eventSlideIn 0.2s ease-out ${delay}ms both`;
        });
    }

    // ── Theme: mode (light/dark/system) + color theme ────────────────────
    // Stored per browser. The inline script in index.html applies the saved
    // values before first paint; this module owns every later change.
    const THEME_MODES = ["light", "dark", "system"];
    const THEME_COLORS = ["orange", "ink", "teal", "blue", "navy", "olive", "coffee", "steel"];
    const systemDark = window.matchMedia("(prefers-color-scheme: dark)");

    function readStored(key, allowed, fallback) {
        try {
            const value = localStorage.getItem(key);
            return allowed.includes(value) ? value : fallback;
        } catch (e) {
            return fallback;
        }
    }

    function writeStored(key, value) {
        try {
            localStorage.setItem(key, value);
        } catch (e) {
            // Storage unavailable (private mode): the choice lasts for this page only
        }
    }

    let themeMode = readStored("fa-mode", THEME_MODES, "system");
    let themeColor = readStored("fa-theme", THEME_COLORS, "orange");

    function applyTheme() {
        const dark = themeMode === "dark" || (themeMode === "system" && systemDark.matches);
        const root = document.documentElement;
        root.dataset.mode = dark ? "dark" : "light";
        root.dataset.theme = themeColor;
        window.dispatchEvent(
            new CustomEvent("themechange", { detail: { mode: themeMode, color: themeColor, dark } })
        );
    }

    const theme = {
        modes: THEME_MODES,
        colors: THEME_COLORS,
        getMode: () => themeMode,
        getColor: () => themeColor,
        setMode(mode) {
            if (!THEME_MODES.includes(mode)) return;
            themeMode = mode;
            writeStored("fa-mode", mode);
            applyTheme();
        },
        setColor(color) {
            if (!THEME_COLORS.includes(color)) return;
            themeColor = color;
            writeStored("fa-theme", color);
            applyTheme();
        },
    };

    systemDark.addEventListener("change", () => {
        if (themeMode === "system") applyTheme();
    });

    // A background tab may miss the change event: re-check when it is shown again
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState !== "visible" || themeMode !== "system") return;
        const shownDark = document.documentElement.dataset.mode === "dark";
        if (shownDark !== systemDark.matches) applyTheme();
    });

    applyTheme();

    // ── Tab navigation ───────────────────────────────────────────────────
    const tabNav = document.getElementById("tabNav");
    const tabBtns = tabNav.querySelectorAll(".tab-btn");
    const panels = document.querySelectorAll(".panel");
    const indicator = document.getElementById("tabIndicator");

    function updateIndicator(btn, immediate = false) {
        if (!btn || !indicator) return;
        const prefersReducedMotion = window.matchMedia &&
            window.matchMedia("(prefers-reduced-motion: reduce)").matches;

        if (immediate || prefersReducedMotion) {
            indicator.style.transition = "none";
        } else {
            indicator.style.transition = "left var(--transition), width var(--transition)";
        }

        indicator.style.left = btn.offsetLeft + "px";
        indicator.style.width = btn.offsetWidth + "px";

        if (immediate && !prefersReducedMotion) {
            void indicator.offsetWidth;
            indicator.style.transition = "left var(--transition), width var(--transition)";
        }
    }

    // ── Route: #<tab>[/<arg>] keeps the open tab and its argument in the URL ──
    const TAB_TITLES = {
        monitor: "Live Monitor",
        attendance: "Attendance",
        dashboard: "Dashboard",
        register: "Register",
        settings: "Settings",
    };

    function parseRoute() {
        let raw = window.location.hash.replace(/^#\/?/, "");
        try {
            raw = decodeURIComponent(raw);
        } catch (e) {
            raw = "";
        }
        const [tab, arg] = raw.split("/");
        const known = Object.prototype.hasOwnProperty.call(TAB_TITLES, tab);
        return { tab: known ? tab : "monitor", arg: known ? arg || "" : "", known: known || raw === "" };
    }

    function writeRoute(tab, arg, replace) {
        const hash = "#" + tab + (arg ? "/" + encodeURIComponent(arg) : "");
        if (window.location.hash === hash) return;
        if (replace) {
            history.replaceState(null, "", hash);
        } else {
            history.pushState(null, "", hash);
        }
    }

    // Lets a tab store one argument (for example the attendance date) in the URL
    function setRouteArg(tab, arg) {
        const active = tabNav.querySelector(".tab-btn.active");
        if (active && active.dataset.tab === tab) writeRoute(tab, arg, true);
    }

    function navigateTo(tabName, opts) {
        const btn = tabNav.querySelector(`.tab-btn[data-tab="${tabName}"]`);
        if (!btn) return;

        tabBtns.forEach((b) => {
            b.classList.remove("active");
            b.setAttribute("aria-selected", "false");
        });
        btn.classList.add("active");
        btn.setAttribute("aria-selected", "true");
        // On a narrow screen the tab bar scrolls: keep the open tab in view
        btn.scrollIntoView({ block: "nearest", inline: "nearest" });
        updateIndicator(btn, false);

        panels.forEach((p) => p.classList.remove("active"));
        const panel = document.getElementById("panel-" + tabName);
        if (panel) panel.classList.add("active");

        // Keep the open tab in the URL so reload, back and bookmarks work
        if (!opts || !opts.fromRoute) writeRoute(tabName, "", false);
        document.title = TAB_TITLES[tabName] + " | Face Insight";

        // Dispatch custom event for cross-tab navigation
        window.dispatchEvent(
            new CustomEvent("tab:navigate", { detail: { tab: tabName, opts: opts || {} } })
        );

        // If navigating to Live Monitor and it's visible, clear event badge
        if (tabName === "monitor") {
            clearEventBadge();
        }
    }

    tabBtns.forEach((btn) => {
        btn.addEventListener("click", () => navigateTo(btn.dataset.tab));
    });

    // Set initial indicator position without transition flash
    const initialBtn = tabNav.querySelector(".tab-btn.active");
    if (initialBtn) updateIndicator(initialBtn, true);

    window.addEventListener("resize", () => {
        const active = tabNav.querySelector(".tab-btn.active");
        if (active) updateIndicator(active, true);
    });

    // Open the tab named in the URL once every script has registered its listeners
    window.addEventListener("DOMContentLoaded", () => {
        const route = parseRoute();
        if (route.tab !== "monitor") {
            navigateTo(route.tab, { fromRoute: true });
        } else {
            document.title = TAB_TITLES.monitor + " | Face Insight";
        }
        if (!route.known) writeRoute(route.tab, "", true);
    });

    // Back/forward buttons and hand-edited URLs
    window.addEventListener("hashchange", () => {
        const route = parseRoute();
        const active = tabNav.querySelector(".tab-btn.active");
        if (!active || active.dataset.tab !== route.tab) {
            navigateTo(route.tab, { fromRoute: true });
        }
        // An unknown tab name falls back to Live Monitor: make the URL say so
        if (!route.known) writeRoute(route.tab, "", true);
        window.dispatchEvent(new CustomEvent("route:change", { detail: route }));
    });

    // ── Connection status ────────────────────────────────────────────────
    const connDot = document.getElementById("connectionDot");
    const connLabel = document.getElementById("connectionLabel");

    socket.on("connect", () => {
        connDot.classList.add("connected");
        connLabel.textContent = "Connected";
    });

    socket.on("disconnect", () => {
        connDot.classList.remove("connected");
        connLabel.textContent = "Disconnected";
    });

    // ── Database status ──────────────────────────────────────────────────
    // null = not reported yet; true/false = last value the server sent.
    // Other scripts read App.db.isConnected() and listen for "db:status".
    let dbConnected = null;
    const dbBanner = document.getElementById("dbBanner");
    const btnDbRetry = document.getElementById("btnDbRetry");
    const btnDbSettings = document.getElementById("btnDbSettings");

    function setDbConnected(value) {
        if (typeof value !== "boolean" || value === dbConnected) return;
        const previous = dbConnected;
        dbConnected = value;
        dbBanner.hidden = value;
        window.dispatchEvent(
            new CustomEvent("db:status", { detail: { connected: value, previous } })
        );
    }

    btnDbRetry.addEventListener("click", () => {
        btnDbRetry.disabled = true;
        btnDbRetry.textContent = "Retrying...";
        fetch("/api/db/reconnect", { method: "POST" })
            .then((r) => r.json())
            .then((data) => {
                setDbConnected(data.db_connected);
                if (data.db_connected) {
                    showToast("Database connected", "success");
                } else {
                    showToast("Still unreachable. Check the connection URI in Settings.", "error", 5000);
                }
            })
            .catch(() => showToast("Could not reach the server", "error"))
            .finally(() => {
                btnDbRetry.disabled = false;
                btnDbRetry.textContent = "Retry connection";
            });
    });

    btnDbSettings.addEventListener("click", () => navigateTo("settings"));

    // ── Live Monitor: Camera controls ────────────────────────────────────
    const cameraSelect = document.getElementById("cameraSelect");
    const btnStart = document.getElementById("btnCameraStart");
    const btnStop = document.getElementById("btnCameraStop");
    const btnFeedStart = document.getElementById("btnFeedStart");
    const statusDot = document.getElementById("cameraStatusDot");
    const videoFeed = document.getElementById("videoFeed");
    const feedEmpty = document.getElementById("feedEmpty");

    // Populate camera dropdown
    fetch("/api/camera/list")
        .then((r) => r.json())
        .then((data) => {
            cameraSelect.innerHTML = "";
            (data.cameras || []).forEach((idx) => {
                const opt = document.createElement("option");
                opt.value = idx;
                opt.textContent = "Camera " + idx;
                cameraSelect.appendChild(opt);
            });
        })
        .catch(() => {
            cameraSelect.innerHTML = '<option value="0">Camera 0</option>';
        });

    const feedEmptyText = document.getElementById("feedEmptyText");
    const feedError = document.getElementById("feedError");
    let cameraRunning = false;
    let cameraStartTimer = null;

    function setCameraPending(pending) {
        clearTimeout(cameraStartTimer);
        cameraStartTimer = null;
        btnStart.disabled = pending || cameraRunning;
        btnFeedStart.disabled = pending;
        feedEmptyText.textContent = pending ? "Starting camera..." : "No camera active";
    }

    // Stays in the feed area until the next attempt, unlike a toast
    function showCameraError(message) {
        feedError.textContent = message;
        feedError.hidden = !message;
    }

    function startCamera() {
        const camIndex = parseInt(cameraSelect.value, 10) || 0;
        showCameraError("");
        setCameraPending(true);
        // The feed appears when camera:status arrives. If it never does, say so.
        cameraStartTimer = setTimeout(() => {
            setCameraPending(false);
            showCameraError("The camera did not respond. Check that it is connected and not in use by another app.");
        }, 10000);

        fetch("/api/camera/start", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
            body: JSON.stringify({ cam_index: camIndex }),
        })
            .then(async (r) => {
                if (r.ok) return;
                const data = await r.json().catch(() => ({}));
                setCameraPending(false);
                showCameraError(data.error || "The camera could not be started.");
            })
            .catch(() => {
                setCameraPending(false);
                showCameraError("Could not reach the server.");
            });
    }

    function stopCamera() {
        fetch("/api/camera/stop", {
            method: "POST",
            headers: { "Content-Type": "application/json" },
        }).catch((err) => showToast("Failed to stop camera: " + err, "error"));
    }

    btnStart.addEventListener("click", startCamera);
    btnStop.addEventListener("click", stopCamera);
    btnFeedStart.addEventListener("click", startCamera);

    // camera:status event - UI updates only from this event, never HTTP
    socket.on("camera:status", (data) => {
        const running = data.running;
        cameraRunning = !!running;
        setCameraPending(false);
        if (running) {
            showCameraError("");
            statusDot.classList.add("active");
            btnStart.disabled = true;
            btnStop.disabled = false;
            feedEmpty.style.display = "none";
            videoFeed.style.display = "block";
            // Point to MJPEG stream with cache-bust
            videoFeed.src = "/video_feed?" + Date.now();
        } else {
            statusDot.classList.remove("active");
            btnStart.disabled = false;
            btnStop.disabled = true;
            feedEmpty.style.display = "flex";
            videoFeed.style.display = "none";
            videoFeed.src = "";
        }
    });

    socket.on("camera:error", (data) => {
        const message = data.message || "Camera error";
        showCameraError(message);
        // The inline message is only visible on Live Monitor
        if (!document.getElementById("panel-monitor").classList.contains("active")) {
            showToast(message, "error");
        }
    });

    // ── Live Monitor: Stats ──────────────────────────────────────────────
    const statFps = document.getElementById("statFps");
    const statFaces = document.getElementById("statFaces");
    const statKnown = document.getElementById("statKnown");
    const statPresent = document.getElementById("statPresent");

    function applyStats(data) {
        if (!data) return;
        setDbConnected(data.db_connected);
        if (data.fps !== undefined) statFps.textContent = data.fps;
        if (data.faces !== undefined) setKpi(statFaces, data.faces);
        if (data.known !== undefined) setKpi(statKnown, data.known);
        if (dbConnected === false) {
            // This count comes from the database: show no number rather than a false zero
            setKpi(statPresent, "--");
        } else if (data.present !== undefined || data.late !== undefined) {
            setKpi(statPresent, (data.present || 0) + (data.late || 0));
        }
    }

    socket.on("stats:update", applyStats);

    // Poll stats periodically (the response also carries the database status)
    setInterval(() => {
        fetch("/api/stats/today")
            .then((r) => r.json())
            .then(applyStats)
            .catch(() => {});
    }, 2000);

    // ── Live Monitor: Auto-Attend toggle ─────────────────────────────────
    const autoAttendToggle = document.getElementById("autoAttendToggle");
    const autoAttendLabel = document.getElementById("autoAttendLabel");

    const autoAttendSaved = document.getElementById("autoAttendSaved");
    const thresholdSaved = document.getElementById("thresholdSaved");

    function flashStatus(el, text) {
        el.textContent = text;
        clearTimeout(el._flashTimer);
        el._flashTimer = setTimeout(() => {
            el.textContent = "";
        }, 2500);
    }

    // These two controls save on change. The server acknowledges each one,
    // so "Saved" is only shown when it really was.
    function emitSetting(event, payload, statusEl) {
        socket.timeout(4000).emit(event, payload, (err, ack) => {
            const ok = !err && ack && ack.ok;
            flashStatus(statusEl, ok ? "Saved" : "Not saved");
            if (!ok) showToast((ack && ack.error) || "The setting could not be saved", "error");
        });
    }

    autoAttendToggle.addEventListener("change", () => {
        const enabled = autoAttendToggle.checked;
        autoAttendLabel.textContent = enabled ? "Enabled" : "Disabled";
        emitSetting("settings:auto_attend", { enabled }, autoAttendSaved);
    });

    // ── Live Monitor: Confidence threshold slider ────────────────────────
    const thresholdSlider = document.getElementById("thresholdSlider");
    const thresholdValue = document.getElementById("thresholdValue");

    // Always read from .value - never a separately cached figure
    thresholdSlider.addEventListener("input", () => {
        thresholdValue.textContent = thresholdSlider.value;
    });

    function announceThreshold() {
        window.dispatchEvent(new CustomEvent("threshold:change", { detail: { value: getThreshold() } }));
    }

    function getThreshold() {
        return parseInt(thresholdSlider.value, 10);
    }

    thresholdSlider.addEventListener("change", () => {
        thresholdValue.textContent = thresholdSlider.value;
        emitSetting("settings:threshold", { value: getThreshold() }, thresholdSaved);
        announceThreshold();
    });

    // Load initial threshold from config
    fetch("/api/config")
        .then((r) => r.json())
        .then((cfg) => {
            if (cfg.confidence_threshold !== undefined) {
                thresholdSlider.value = cfg.confidence_threshold;
                thresholdValue.textContent = thresholdSlider.value;
                announceThreshold();
            }
            if (cfg.attendance && cfg.attendance.auto_attend !== undefined) {
                autoAttendToggle.checked = cfg.attendance.auto_attend;
                autoAttendLabel.textContent = cfg.attendance.auto_attend ? "Enabled" : "Disabled";
            }
        })
        .catch(() => {});

    // ── Live Monitor: Screenshot ──────────────────────────────────────────
    const btnScreenshot = document.getElementById("btnScreenshot");

    btnScreenshot.addEventListener("click", () => {
        if (!videoFeed.src || videoFeed.style.display === "none") {
            showToast("No camera feed active", "error");
            return;
        }
        const canvas = document.createElement("canvas");
        canvas.width = videoFeed.naturalWidth || videoFeed.width;
        canvas.height = videoFeed.naturalHeight || videoFeed.height;
        const ctx = canvas.getContext("2d");
        ctx.drawImage(videoFeed, 0, 0);
        const link = document.createElement("a");
        link.download = "screenshot_" + new Date().toISOString().replace(/[:.]/g, "-") + ".png";
        link.href = canvas.toDataURL("image/png");
        link.click();
        showToast("Screenshot saved", "success");
    });

    // ── Live Monitor: Event Feed ─────────────────────────────────────────
    const eventFeed = document.getElementById("eventFeed");
    const eventBadge = document.getElementById("eventBadge");
    let unreadCount = 0;
    const MAX_EVENTS = 50;

    function isMonitorVisible() {
        const monitorPanel = document.getElementById("panel-monitor");
        return (
            monitorPanel &&
            monitorPanel.classList.contains("active") &&
            document.visibilityState === "visible" &&
            document.hasFocus()
        );
    }

    function clearEventBadge() {
        unreadCount = 0;
        eventBadge.style.display = "none";
        setKpi(eventBadge, 0);
    }

    function createEventItemElement(record) {
        const status = (record.status || "unknown").toLowerCase();
        const dotClass =
            status === "present" ? "event-item__dot--present" :
            status === "late" ? "event-item__dot--late" :
            "event-item__dot--unknown";

        const item = document.createElement("button");
        item.type = "button";
        item.className = "event-item";
        item.title = "Open this record";
        if (record.id) {
            item.dataset.id = record.id;
            item.addEventListener("click", () => navigateTo("attendance", { recordId: record.id }));
        }
        item.innerHTML = `
            <span class="event-item__dot ${dotClass}"></span>
            <div class="event-item__info">
                <div class="event-item__name">${escapeHtml(record.name || "Unknown")}</div>
                <div class="event-item__meta">${escapeHtml(record.emotion || "")} &middot; ${escapeHtml(record.status || "")}</div>
            </div>
            <span class="event-item__time">${escapeHtml(record.time_in || "")}</span>
        `;
        return item;
    }

    function addEventItem(record) {
        // Remove empty placeholder
        const emptyMsg = eventFeed.querySelector(".event-feed__empty");
        if (emptyMsg) emptyMsg.remove();

        const item = createEventItemElement(record);
        // Single live row animates in on its own without stagger delay
        const prefersReducedMotion = window.matchMedia &&
            window.matchMedia("(prefers-reduced-motion: reduce)").matches;
        item.style.animation = prefersReducedMotion ? "none" : "eventSlideIn 0.2s ease-out 0ms both";

        // Insert at top (newest first)
        eventFeed.prepend(item);

        // Cap total items
        const items = eventFeed.querySelectorAll(".event-item");
        if (items.length > MAX_EVENTS) {
            items[items.length - 1].remove();
        }

        // Badge management
        if (!isMonitorVisible()) {
            unreadCount++;
            setKpi(eventBadge, unreadCount);
            eventBadge.style.display = "inline-flex";
        }
    }

    // Populate the event feed from today's records, newest first
    function loadEventFeed() {
        fetch("/api/attendance")
            .then((r) => r.json())
            .then((records) => {
                if (!Array.isArray(records) || records.length === 0) return;
                eventFeed.innerHTML = "";
                [...records]
                    .sort((a, b) => (b.time_in || "").localeCompare(a.time_in || ""))
                    .slice(0, MAX_EVENTS)
                    .forEach((rec) => eventFeed.appendChild(createEventItemElement(rec)));
                staggerIn(eventFeed.querySelectorAll(".event-item"));
            })
            .catch(() => {});
    }

    loadEventFeed();

    window.addEventListener("db:status", (e) => {
        if (e.detail.connected && e.detail.previous === false) loadEventFeed();
    });

    socket.on("attendance:new", (record) => {
        addEventItem(record);
    });

    socket.on("attendance:deleted", (data) => {
        const ids = new Set((data && data.ids) || []);
        eventFeed.querySelectorAll(".event-item").forEach((item) => {
            if (ids.has(item.dataset.id)) item.remove();
        });
        if (!eventFeed.querySelector(".event-item")) {
            eventFeed.innerHTML = '<p class="event-feed__empty">No events yet</p>';
        }
    });

    // Clear badge when page regains focus while on monitor tab
    document.addEventListener("visibilitychange", () => {
        if (document.visibilityState === "visible" && isMonitorVisible()) {
            clearEventBadge();
        }
    });

    window.addEventListener("focus", () => {
        if (isMonitorVisible()) {
            clearEventBadge();
        }
    });

    // ── Utility ──────────────────────────────────────────────────────────
    function escapeHtml(str) {
        const div = document.createElement("div");
        div.textContent = str;
        return div.innerHTML;
    }

    // ── Expose shared helpers globally for later phase scripts ────────────
    window.App = {
        socket,
        theme,
        route: { get: parseRoute, setArg: setRouteArg },
        db: { isConnected: () => dbConnected },
        getThreshold,
        trapFocus,
        confirm: confirmDialog,
        download,
        showToast,
        setKpi,
        staggerIn,
        navigateTo,
        escapeHtml,
    };
})();
