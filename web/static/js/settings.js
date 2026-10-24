/* ==========================================================================
   settings.js - Settings Tab
   Appearance, database config, Firebase sync, attendance rules, groups and
   their schedules, accounts and the danger zone. Names live on the People page.
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
    const inputReviewMargin = document.getElementById("settingsReviewMargin");

    const errCooldown = document.getElementById("errCooldown");
    const errReviewMargin = document.getElementById("errReviewMargin");
    const errMongoUri = document.getElementById("errMongoUri");
    const errMongoDb = document.getElementById("errMongoDb");

    const btnSave = document.getElementById("btnSaveSettings");
    const saveStatus = document.getElementById("settingsSaveStatus");

    const btnDiscard = document.getElementById("btnDiscardSettings");
    const saveBar = document.querySelector(".settings-save-bar");
    const settingsPanel = document.getElementById("panel-settings");
    const settingsTabBtn = document.querySelector('.tab-btn[data-tab="settings"]');

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
                review_margin: parseInt(inputReviewMargin.value, 10),
            },
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
            settingsTabBtn.title = "Settings (unsaved changes)";
            saveStatus.textContent = "Unsaved changes";
        } else {
            settingsTabBtn.title = "Settings";
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

    // ── Appearance (mode, colour theme, font; saved to the account) ──────
    const modeButtons = document.querySelectorAll("[data-mode-option]");
    const themeButtons = document.querySelectorAll("[data-theme-option]");
    const fontButtons = document.querySelectorAll("[data-font-option]");

    function syncAppearance() {
        const theme = window.App.theme;
        modeButtons.forEach((btn) => {
            btn.setAttribute("aria-pressed", String(btn.dataset.modeOption === theme.getMode()));
        });
        themeButtons.forEach((btn) => {
            btn.setAttribute("aria-pressed", String(btn.dataset.themeOption === theme.getColor()));
        });
        fontButtons.forEach((btn) => {
            btn.setAttribute("aria-pressed", String(btn.dataset.fontOption === theme.getFont()));
        });
    }

    modeButtons.forEach((btn) => {
        btn.addEventListener("click", () => window.App.theme.setMode(btn.dataset.modeOption));
    });
    themeButtons.forEach((btn) => {
        btn.addEventListener("click", () => window.App.theme.setColor(btn.dataset.themeOption));
    });
    fontButtons.forEach((btn) => {
        btn.addEventListener("click", () => window.App.theme.setFont(btn.dataset.fontOption));
    });

    // ── Accounts ─────────────────────────────────────────────────────────
    const usersTableBody = document.getElementById("usersTableBody");
    const addUserForm = document.getElementById("addUserForm");
    const passwordForm = document.getElementById("passwordForm");
    const errAddUser = document.getElementById("errAddUser");
    const errPassword = document.getElementById("errPassword");

    function emptyRow() {
        usersTableBody.innerHTML = `<tr><td colspan="2" class="table-empty"></td></tr>`;
        return usersTableBody.querySelector("td");
    }

    function renderUsers(list) {
        if (!list.length) {
            window.App.renderState(emptyRow(), "empty", "No accounts.");
            return;
        }
        usersTableBody.innerHTML = list
            .map((u) => `
                <tr>
                    <td>${window.App.escapeHtml(u.username)}</td>
                    <td class="mono">${u.last_login ? window.App.escapeHtml(u.last_login.replace("T", " ")) : "Never"}</td>
                </tr>`)
            .join("");
    }

    async function loadUsers() {
        window.App.renderState(emptyRow(), "loading", "Loading...");
        try {
            const res = await fetch("/api/users");
            if (!res.ok) throw new Error("users");
            renderUsers(await res.json());
        } catch (err) {
            window.App.renderState(emptyRow(), "error", "Could not load accounts.", loadUsers);
        }
    }

    addUserForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        errAddUser.textContent = "";
        const username = document.getElementById("newUserName").value.trim();
        const password = document.getElementById("newUserPassword").value;
        const submit = addUserForm.querySelector('button[type="submit"]');
        submit.disabled = true;
        try {
            const res = await window.App.fetchFresh("/api/users", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ username, password }),
            });
            if (!res) return; // password not confirmed
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                errAddUser.textContent = data.error || "The account could not be added.";
                return;
            }
            addUserForm.reset();
            renderUsers(data.users || []);
            window.App.showToast(`Added ${username}. They can sign in now.`, "success");
        } catch (err) {
            errAddUser.textContent = "Could not reach the server.";
        } finally {
            submit.disabled = false;
        }
    });

    passwordForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        errPassword.textContent = "";
        const current = document.getElementById("pwCurrent").value;
        const next = document.getElementById("pwNew").value;
        if (next !== document.getElementById("pwConfirm").value) {
            errPassword.textContent = "The two new passwords do not match.";
            return;
        }
        const submit = passwordForm.querySelector('button[type="submit"]');
        submit.disabled = true;
        try {
            const res = await fetch("/api/auth/password", {
                method: "POST",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ current, new: next }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                errPassword.textContent = data.error || "The password was not changed.";
                return;
            }
            passwordForm.reset();
            window.App.showToast("Password changed.", "success");
        } catch (err) {
            errPassword.textContent = "Could not reach the server.";
        } finally {
            submit.disabled = false;
        }
    });

    loadUsers();

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
            inputReviewMargin.value = att.review_margin !== undefined ? att.review_margin : 15;

            savedSnapshot = JSON.stringify(currentPayload());
            updateDirtyUi();
        } catch (err) {
            console.error("loadConfig error:", err);
            window.App.showToast("Failed to load settings", "error");
        }
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
            const margin = parseInt(inputReviewMargin.value, 10);

            let clientValid = true;

            if (isNaN(cooldown) || cooldown < 1) {
                if (errCooldown) errCooldown.textContent = "Cooldown must be an integer >= 1";
                clientValid = false;
            }

            if (isNaN(margin) || margin < 0 || margin > 50) {
                errReviewMargin.textContent = "Review margin must be a whole number from 0 to 50";
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
                    } else if (data.field === "review_margin") {
                        errReviewMargin.textContent = data.error;
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

    // ── Groups and schedules ─────────────────────────────────────────────
    // Each group has working days, a start, a late cut-off and an end. The
    // Default group always exists. Saved on its own, not with the form above.
    const WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"];
    const schedulesList = document.getElementById("schedulesList");
    const newGroupName = document.getElementById("newGroupName");
    const btnAddGroup = document.getElementById("btnAddGroup");
    const btnSaveSchedules = document.getElementById("btnSaveSchedules");
    const errSchedules = document.getElementById("errSchedules");
    const schedulesStatus = document.getElementById("schedulesStatus");
    let groups = [];        // [{name, working_days, start, late_after, end, members}]
    let groupsSaved = "";   // JSON of the groups as last loaded or saved

    function groupsJson() {
        const out = {};
        groups.forEach((g) => {
            out[g.name] = { working_days: g.working_days, start: g.start, late_after: g.late_after, end: g.end };
        });
        return JSON.stringify(out);
    }

    function updateGroupsDirty() {
        const dirty = groupsJson() !== groupsSaved;
        btnSaveSchedules.disabled = !dirty;
        schedulesStatus.textContent = dirty ? "Unsaved changes" : schedulesStatus.textContent === "Unsaved changes" ? "" : schedulesStatus.textContent;
    }

    function renderGroups() {
        const esc = window.App.escapeHtml;
        schedulesList.innerHTML = groups
            .map((g, i) => `
                <fieldset class="schedule-row" data-index="${i}">
                    <legend class="schedule-row__name">${esc(g.name)}
                        <span class="schedule-row__members">${g.members} ${g.members === 1 ? "person" : "people"}</span>
                    </legend>
                    <div class="check-chips" role="group" aria-label="Working days for ${esc(g.name)}">
                        ${WEEKDAYS.map((d, n) => `<label class="check-chip"><input type="checkbox" data-day="${n}" ${g.working_days.includes(n) ? "checked" : ""}><span>${d}</span></label>`).join("")}
                    </div>
                    <div class="schedule-row__times">
                        <label>Start <input type="time" class="form-input" data-field="start" value="${esc(g.start)}"></label>
                        <label>Late after <input type="time" class="form-input" data-field="late_after" value="${esc(g.late_after)}"></label>
                        <label>End <input type="time" class="form-input" data-field="end" value="${esc(g.end)}"></label>
                        ${g.name === "Default"
                            ? '<span class="form-hint">Everyone starts here.</span>'
                            : `<button type="button" class="btn btn--ghost btn--sm" data-remove ${g.members ? `disabled title="Move its ${g.members} ${g.members === 1 ? "person" : "people"} to another group first"` : ""}>Remove</button>`}
                    </div>
                </fieldset>`)
            .join("");
    }

    schedulesList.addEventListener("change", (e) => {
        const row = e.target.closest(".schedule-row");
        if (!row) return;
        const g = groups[Number(row.dataset.index)];
        if (e.target.dataset.day !== undefined) {
            const day = Number(e.target.dataset.day);
            g.working_days = e.target.checked
                ? [...new Set([...g.working_days, day])].sort()
                : g.working_days.filter((d) => d !== day);
        } else if (e.target.dataset.field) {
            g[e.target.dataset.field] = e.target.value;
        }
        updateGroupsDirty();
    });

    schedulesList.addEventListener("click", (e) => {
        const btn = e.target.closest("[data-remove]");
        if (!btn) return;
        groups.splice(Number(btn.closest(".schedule-row").dataset.index), 1);
        renderGroups();
        updateGroupsDirty();
    });

    btnAddGroup.addEventListener("click", () => {
        errSchedules.textContent = "";
        const name = newGroupName.value.trim().replace(/\s+/g, " ");
        if (!name) {
            errSchedules.textContent = "Type a name for the new group.";
            return;
        }
        if (groups.some((g) => g.name.toLowerCase() === name.toLowerCase())) {
            errSchedules.textContent = `There is already a group called ${name}.`;
            return;
        }
        const base = groups[0];
        groups.push({ name, working_days: [...base.working_days], start: base.start, late_after: base.late_after, end: base.end, members: 0 });
        newGroupName.value = "";
        renderGroups();
        updateGroupsDirty();
    });

    async function loadGroups() {
        try {
            const res = await fetch("/api/schedules");
            if (!res.ok) throw new Error("schedules");
            groups = (await res.json()).groups || [];
            groupsSaved = groupsJson();
            renderGroups();
            updateGroupsDirty();
        } catch (err) {
            window.App.renderState(schedulesList, "error", "Could not load the groups.", loadGroups);
        }
    }

    btnSaveSchedules.addEventListener("click", async () => {
        errSchedules.textContent = "";
        for (const g of groups) {
            if (!g.working_days.length) {
                errSchedules.textContent = `${g.name}: pick at least one working day.`;
                return;
            }
            if (!(g.start && g.late_after && g.end && g.start <= g.late_after && g.late_after < g.end)) {
                errSchedules.textContent = `${g.name}: times must run in order: start, then late after, then end.`;
                return;
            }
        }
        btnSaveSchedules.disabled = true;
        try {
            const res = await fetch("/api/schedules", {
                method: "PUT",
                headers: { "Content-Type": "application/json" },
                body: JSON.stringify({ groups: JSON.parse(groupsJson()) }),
            });
            const data = await res.json().catch(() => ({}));
            if (!res.ok) {
                errSchedules.textContent = data.error || "The groups were not saved.";
                updateGroupsDirty();
                return;
            }
            groups = data.groups;
            groupsSaved = groupsJson();
            renderGroups();
            updateGroupsDirty();
            schedulesStatus.textContent = `Saved at ${new Date().toLocaleTimeString()}`;
            window.dispatchEvent(new CustomEvent("schedules:change"));
        } catch (err) {
            errSchedules.textContent = "Could not reach the server.";
            updateGroupsDirty();
        }
    });

    loadGroups();
    window.addEventListener("tab:navigate", (e) => {
        if (e.detail && e.detail.tab === "settings" && groupsJson() === groupsSaved) loadGroups();
    });

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

        const res = await window.App.fetchFresh(`/api/attendance?date=${encodeURIComponent(today)}`, { method: "DELETE" });
        if (!res) return; // password not confirmed
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
