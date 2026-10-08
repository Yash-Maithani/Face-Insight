/* ==========================================================================
   settings.js - Settings Tab
   Appearance, database config, Firebase sync, attendance rules, and
   Name/ID manager.
   ========================================================================== */

(function () {
    "use strict";

    // DOM Elements
    const inputMongoUri = document.getElementById("settingsMongoUri");
    const inputMongoDb = document.getElementById("settingsMongoDb");
    const btnTestMongo = document.getElementById("btnTestMongo");
    const mongoTestResult = document.getElementById("mongoTestResult");

    const inputFirebaseUrl = document.getElementById("settingsFirebaseUrl");
    const inputFirebaseCred = document.getElementById("settingsFirebaseCred");

    const inputCooldown = document.getElementById("settingsCooldown");
    const inputLateAfter = document.getElementById("settingsLateAfter");
    const inputWorkingEnd = document.getElementById("settingsWorkingEnd");

    const errCooldown = document.getElementById("errCooldown");
    const errLateAfter = document.getElementById("errLateAfter");
    const errWorkingEnd = document.getElementById("errWorkingEnd");
    const errMongoUri = document.getElementById("errMongoUri");
    const errMongoDb = document.getElementById("errMongoDb");
    const errNewName = document.getElementById("errNewName");

    const inputNewNameId = document.getElementById("newNameId");
    const inputNewNameVal = document.getElementById("newNameVal");
    const btnAddName = document.getElementById("btnAddName");
    const namesTableBody = document.getElementById("namesTableBody");

    const btnSave = document.getElementById("btnSaveSettings");
    const saveStatus = document.getElementById("settingsSaveStatus");

    const btnDiscard = document.getElementById("btnDiscardSettings");
    const saveBar = document.querySelector(".settings-save-bar");
    const settingsPanel = document.getElementById("panel-settings");
    const settingsTabBtn = document.querySelector('.tab-btn[data-tab="settings"]');

    // Local state for names dictionary { [id: string]: string }
    let localNames = {};

    // ── Unsaved changes ──────────────────────────────────────────────────
    // savedSnapshot is the form as last loaded or saved. While the form
    // differs from it, the tab is not reloaded and leaving the page warns.
    let savedSnapshot = null;

    function currentPayload() {
        return {
            mongodb_uri: inputMongoUri.value.trim(),
            mongodb_db: inputMongoDb.value.trim(),
            firebase_url: inputFirebaseUrl.value.trim(),
            firebase_credentials: inputFirebaseCred.value.trim(),
            attendance: {
                cooldown_minutes: parseInt(inputCooldown.value, 10),
                late_after: inputLateAfter.value,
                working_hours_end: inputWorkingEnd.value,
            },
            names: localNames,
        };
    }

    function isDirty() {
        return savedSnapshot !== null && JSON.stringify(currentPayload()) !== savedSnapshot;
    }

    function updateDirtyUi() {
        const dirty = isDirty();
        saveBar.classList.toggle("settings-save-bar--dirty", dirty);
        settingsTabBtn.classList.toggle("tab-btn--dirty", dirty);
        btnDiscard.hidden = !dirty;
        if (dirty) {
            settingsTabBtn.title = "Unsaved changes";
            saveStatus.textContent = "Unsaved changes";
        } else {
            settingsTabBtn.removeAttribute("title");
            if (saveStatus.textContent === "Unsaved changes") saveStatus.textContent = "";
        }
    }

    settingsPanel.addEventListener("input", updateDirtyUi);
    settingsPanel.addEventListener("change", updateDirtyUi);

    btnDiscard.addEventListener("click", async () => {
        await loadConfig();
        window.App.showToast("Changes discarded", "info");
    });

    window.addEventListener("beforeunload", (e) => {
        if (isDirty()) {
            e.preventDefault();
            e.returnValue = "";
        }
    });

    // ── Appearance (mode + color theme, stored per browser) ──────────────
    const modeButtons = document.querySelectorAll("[data-mode-option]");
    const themeButtons = document.querySelectorAll("[data-theme-option]");

    function syncAppearance() {
        const theme = window.App.theme;
        modeButtons.forEach((btn) => {
            btn.setAttribute("aria-pressed", String(btn.dataset.modeOption === theme.getMode()));
        });
        themeButtons.forEach((btn) => {
            btn.setAttribute("aria-pressed", String(btn.dataset.themeOption === theme.getColor()));
        });
    }

    modeButtons.forEach((btn) => {
        btn.addEventListener("click", () => window.App.theme.setMode(btn.dataset.modeOption));
    });
    themeButtons.forEach((btn) => {
        btn.addEventListener("click", () => window.App.theme.setColor(btn.dataset.themeOption));
    });

    window.addEventListener("themechange", syncAppearance);
    syncAppearance();

    // ── Clear Field Errors ───────────────────────────────────────────────
    function clearErrors() {
        const errorSpans = document.querySelectorAll(".field-error");
        errorSpans.forEach((span) => (span.textContent = ""));
        if (saveStatus) saveStatus.textContent = "";
    }

    // ── Load Config ──────────────────────────────────────────────────────
    async function loadConfig() {
        clearErrors();
        try {
            const res = await fetch("/api/config");
            if (!res.ok) throw new Error("Failed to load configuration");
            const cfg = await res.json();

            if (inputMongoUri) inputMongoUri.value = cfg.mongodb_uri || "";
            if (inputMongoDb) inputMongoDb.value = cfg.mongodb_db || "";
            if (inputFirebaseUrl) inputFirebaseUrl.value = cfg.firebase_url || "";
            if (inputFirebaseCred) inputFirebaseCred.value = cfg.firebase_credentials || "";

            const att = cfg.attendance || {};
            if (inputCooldown) inputCooldown.value = att.cooldown_minutes || 60;
            if (inputLateAfter) inputLateAfter.value = att.late_after || "09:30";
            if (inputWorkingEnd) inputWorkingEnd.value = att.working_hours_end || "17:30";

            localNames = Object.assign({}, cfg.names || {});
            renderNames();
            savedSnapshot = JSON.stringify(currentPayload());
            updateDirtyUi();
        } catch (err) {
            console.error("loadConfig error:", err);
            window.App.showToast("Failed to load settings", "error");
        }
    }

    // ── Names Table Rendering ────────────────────────────────────────────
    function renderNames() {
        if (!namesTableBody) return;

        const keys = Object.keys(localNames).sort((a, b) => parseInt(a, 10) - parseInt(b, 10));
        if (keys.length === 0) {
            namesTableBody.innerHTML = `<tr><td colspan="3" class="table-empty">No names configured yet. Add one above.</td></tr>`;
            return;
        }

        namesTableBody.innerHTML = keys
            .map((id) => {
                const name = localNames[id];
                return `
                <tr data-id="${id}">
                    <td><code>${window.App.escapeHtml(id)}</code></td>
                    <td>
                        <input type="text" class="form-input form-input--sm name-edit-input" data-id="${id}" value="${window.App.escapeHtml(name)}">
                    </td>
                    <td style="text-align:right">
                        <button type="button" class="btn btn--ghost btn--sm btn-remove-name" data-id="${id}" title="Remove name from config (does NOT delete images)">
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                            Remove
                        </button>
                    </td>
                </tr>
            `;
            })
            .join("");

        // Listen for inline edits
        namesTableBody.querySelectorAll(".name-edit-input").forEach((inp) => {
            inp.addEventListener("change", () => {
                const id = inp.dataset.id;
                const val = inp.value.trim();
                if (val) {
                    localNames[id] = val;
                } else {
                    inp.value = localNames[id];
                }
            });
        });

        // Listen for remove buttons
        namesTableBody.querySelectorAll(".btn-remove-name").forEach((btn) => {
            btn.addEventListener("click", () => {
                const id = btn.dataset.id;
                delete localNames[id];
                renderNames();
                updateDirtyUi();
                window.App.showToast(`Removed ID ${id} from names list (image files kept)`, "info");
            });
        });
    }

    // ── Add Name ─────────────────────────────────────────────────────────
    if (btnAddName) {
        btnAddName.addEventListener("click", () => {
            if (errNewName) errNewName.textContent = "";

            const id = inputNewNameId.value.trim();
            const val = inputNewNameVal.value.trim();

            if (!id || isNaN(parseInt(id, 10)) || parseInt(id, 10) < 0) {
                if (errNewName) errNewName.textContent = "ID must be a non-negative integer";
                return;
            }
            if (!val) {
                if (errNewName) errNewName.textContent = "Name cannot be empty";
                return;
            }

            localNames[id] = val;
            inputNewNameId.value = "";
            inputNewNameVal.value = "";
            renderNames();
            updateDirtyUi();
        });
    }

    // ── Test MongoDB Connection ──────────────────────────────────────────
    if (btnTestMongo) {
        btnTestMongo.addEventListener("click", async () => {
            if (!mongoTestResult) return;
            mongoTestResult.className = "db-test-result";
            mongoTestResult.textContent = "Testing connection...";
            btnTestMongo.disabled = true;

            const uri = (inputMongoUri ? inputMongoUri.value : "").trim();
            try {
                const res = await fetch("/api/config/db_test", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ uri }),
                });
                const data = await res.json();
                if (data.ok) {
                    mongoTestResult.className = "db-test-result db-test-result--ok";
                    mongoTestResult.textContent = "Connected.";
                } else {
                    mongoTestResult.className = "db-test-result db-test-result--fail";
                    mongoTestResult.textContent = `Connection failed: ${data.error || "Unreachable"}`;
                }
            } catch (err) {
                mongoTestResult.className = "db-test-result db-test-result--fail";
                mongoTestResult.textContent = "Network error testing connection";
            } finally {
                btnTestMongo.disabled = false;
            }
        });
    }

    // ── Save Settings ────────────────────────────────────────────────────
    if (btnSave) {
        btnSave.addEventListener("click", async () => {
            clearErrors();

            const cooldown = parseInt(inputCooldown.value, 10);
            const lateAfter = inputLateAfter.value;
            const workingEnd = inputWorkingEnd.value;

            let clientValid = true;

            if (isNaN(cooldown) || cooldown < 1) {
                if (errCooldown) errCooldown.textContent = "Cooldown must be an integer >= 1";
                clientValid = false;
            }

            const timePattern = /^([01]\d|2[0-3]):([0-5]\d)$/;
            if (!lateAfter || !timePattern.test(lateAfter)) {
                if (errLateAfter) errLateAfter.textContent = "Late After must be a valid time (HH:MM)";
                clientValid = false;
            }
            if (!workingEnd || !timePattern.test(workingEnd)) {
                if (errWorkingEnd) errWorkingEnd.textContent = "Working Hours End must be a valid time (HH:MM)";
                clientValid = false;
            }

            if (clientValid && lateAfter >= workingEnd) {
                if (errLateAfter) errLateAfter.textContent = "Late After must be earlier than Working Hours End";
                clientValid = false;
            }

            if (!clientValid) {
                window.App.showToast("Please correct the highlighted errors", "error");
                return;
            }

            const payload = currentPayload();

            try {
                btnSave.disabled = true;
                if (saveStatus) saveStatus.textContent = "Saving...";

                const res = await fetch("/api/config", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify(payload),
                });
                const data = await res.json();

                if (!res.ok) {
                    // Display server validation error
                    if (data.field === "cooldown_minutes" && errCooldown) {
                        errCooldown.textContent = data.error;
                    } else if (data.field === "late_after" && errLateAfter) {
                        errLateAfter.textContent = data.error;
                    } else if (data.field === "working_hours_end" && errWorkingEnd) {
                        errWorkingEnd.textContent = data.error;
                    } else if (data.field === "names" && errNewName) {
                        errNewName.textContent = data.error;
                    }
                    window.App.showToast(data.error || "Failed to save settings", "error");
                    if (saveStatus) saveStatus.textContent = "Failed to save";
                    return;
                }

                window.App.showToast("Settings saved and applied.", "success");
                if (data.warning) {
                    window.App.showToast(data.warning, "warning", 5000);
                }
                savedSnapshot = JSON.stringify(currentPayload());
                updateDirtyUi();
                if (saveStatus) saveStatus.textContent = `Saved at ${new Date().toLocaleTimeString()}`;
            } catch (err) {
                window.App.showToast("Network error saving settings", "error");
                if (saveStatus) saveStatus.textContent = "Error saving";
            } finally {
                btnSave.disabled = false;
            }
        });
    }

    // ── Danger zone: delete today's records ──────────────────────────────
    // Not part of the settings form: it acts at once, after a confirmation
    // that states exactly how many records will go.
    const btnClearToday = document.getElementById("btnClearToday");

    function localToday() {
        const d = new Date();
        const pad = (n) => String(n).padStart(2, "0");
        return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}`;
    }

    function plural(n, word) {
        return `${n} ${word}${n === 1 ? "" : "s"}`;
    }

    async function clearToday() {
        const today = localToday();
        const todayLabel = new Date().toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });

        // Look first, so the confirmation can say what is about to be removed
        const [status, list] = await Promise.all([
            fetch("/api/status").then((r) => r.json()),
            fetch(`/api/attendance?date=${encodeURIComponent(today)}`).then((r) => r.json()),
        ]);
        if (!status.db_connected) {
            window.App.showToast("Database offline. Nothing was deleted.", "error");
            return;
        }
        const records = Array.isArray(list) ? list : [];
        if (records.length === 0) {
            window.App.showToast(`There are no records for today (${todayLabel})`, "info");
            return;
        }

        const count = (s) => records.filter((r) => r.status === s).length;
        let message =
            `This deletes ${plural(records.length, "record")} for today, ${todayLabel}: ` +
            `${count("Present")} Present, ${count("Late")} Late, ${count("Unknown")} Unknown. ` +
            "Their face snapshots are deleted too, and everyone can be recorded again straight away. " +
            "This cannot be undone.";
        if (status.firebase_enabled) {
            message += " Copies already sent to Firebase are not removed.";
        }

        const confirmed = await window.App.confirm({
            title: "Delete today's records?",
            message,
            confirmLabel: `Delete ${plural(records.length, "record")}`,
            danger: true,
            extra: {
                label: "Export first",
                onClick: () => {
                    window.App.download(`/api/export/excel?date=${encodeURIComponent(today)}`);
                    window.App.showToast(`Downloading Excel file for ${today}`, "info");
                },
            },
        });
        if (!confirmed) return;

        const res = await fetch(`/api/attendance?date=${encodeURIComponent(today)}`, { method: "DELETE" });
        const data = await res.json().catch(() => ({}));
        if (!res.ok) {
            window.App.showToast(data.error || "Today's records could not be deleted", "error");
            return;
        }
        window.App.showToast(`Deleted ${plural(data.deleted, "record")} for today`, "success");
    }

    if (btnClearToday) {
        // A flag, not `disabled`: the dialog hands focus back to this button
        // when it closes, and a disabled button cannot take focus.
        let clearing = false;
        btnClearToday.addEventListener("click", async () => {
            if (clearing) return;
            clearing = true;
            try {
                await clearToday();
            } catch (err) {
                window.App.showToast("Could not reach the server. Nothing was deleted.", "error");
            } finally {
                clearing = false;
            }
        });
    }

    // ── Tab Navigation listener (reload config when tab opens) ───────────
    window.addEventListener("tab:navigate", (e) => {
        // Reloading would throw away edits, so only refresh a clean form
        if (e.detail && e.detail.tab === "settings" && !isDirty()) {
            loadConfig();
        }
    });

    // Initial load
    loadConfig();
})();
