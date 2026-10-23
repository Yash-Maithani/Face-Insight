/* ==========================================================================
   register.js - Face Registration Tab
   Collection preview, SVG progress ring, model training, and persons list.
   ========================================================================== */

(function () {
    "use strict";

    const LOW_IMAGE_THRESHOLD = 100;
    const RING_CIRCUMFERENCE = 2 * Math.PI * 50; // r = 50 -> ~314.159

    // DOM Elements
    const form = document.getElementById("registerForm");
    const inputPersonId = document.getElementById("registerPersonId");
    const inputName = document.getElementById("registerName");
    const sliderMaxImages = document.getElementById("registerMaxImages");
    const valMaxImages = document.getElementById("registerMaxImagesVal");
    const btnStart = document.getElementById("btnCollectStart");
    const btnStop = document.getElementById("btnCollectStop");
    const earlyStopNotice = document.getElementById("earlyStopNotice");

    const previewImg = document.getElementById("collectPreviewImg");
    const previewEmpty = document.getElementById("collectPreviewEmpty");
    const ringBar = document.getElementById("collectRingBar");
    const ringPct = document.getElementById("collectRingPct");
    const ringCount = document.getElementById("collectRingCount");

    const btnTrainNow = document.getElementById("btnTrainNow");
    const trainProgressWrap = document.getElementById("trainProgressWrap");
    const trainProgressMsg = document.getElementById("trainProgressMsg");
    const trainProgressPct = document.getElementById("trainProgressPct");
    const trainProgressBar = document.getElementById("trainProgressBar");
    const trainLog = document.getElementById("trainLog");
    const trainSummaryCard = document.getElementById("trainSummaryCard");
    const trainSumTotal = document.getElementById("trainSumTotal");
    const trainSumPersons = document.getElementById("trainSumPersons");
    const trainSumSkipped = document.getElementById("trainSumSkipped");
    const trainSumList = document.getElementById("trainSumList");

    const personsTableBody = document.getElementById("personsTableBody");
    const personsCountBadge = document.getElementById("personsCountBadge");

    const idHint = document.getElementById("registerIdHint");

    // State
    let isCollecting = false;
    let isTraining = false;
    let currentTarget = 500;
    let persons = [];        // last list from /api/persons
    let idTouched = false;   // the user typed an ID, so stop suggesting one
    let nameLocked = false;  // name is filled in from an existing person

    // ── Person ID helpers ────────────────────────────────────────────────
    // Suggest the next free ID until the user types their own.
    function suggestNextId() {
        if (idTouched || isCollecting) return;
        const ids = persons.map((p) => p.person_id);
        inputPersonId.value = ids.length ? Math.max(...ids) + 1 : 1;
    }

    // An ID that is already registered keeps its name. Collecting again adds
    // images to that person instead of silently renaming them.
    function syncExistingPerson() {
        const id = parseInt(inputPersonId.value, 10);
        const match = persons.find((p) => p.person_id === id);
        if (match) {
            inputName.value = match.name;
            inputName.readOnly = true;
            nameLocked = true;
            idHint.textContent =
                `ID ${id} is already registered as ${match.name} (${match.image_count} images). ` +
                "New images will be added to this person. To rename someone, use the Name / ID Manager in Settings.";
            idHint.hidden = false;
        } else {
            if (nameLocked) inputName.value = "";
            inputName.readOnly = false;
            nameLocked = false;
            idHint.hidden = true;
        }
    }

    inputPersonId.addEventListener("input", () => {
        idTouched = inputPersonId.value !== "";
        syncExistingPerson();
    });

    // ── Circular Ring Initialization ─────────────────────────────────────
    if (ringBar) {
        ringBar.style.strokeDasharray = `${RING_CIRCUMFERENCE} ${RING_CIRCUMFERENCE}`;
        ringBar.style.strokeDashoffset = RING_CIRCUMFERENCE;
    }

    function setRingProgress(collected, target) {
        if (!ringBar || !ringPct || !ringCount) return;
        const tgt = target > 0 ? target : 1;
        const pct = Math.min(100, Math.round((collected / tgt) * 100));
        const offset = RING_CIRCUMFERENCE - (pct / 100) * RING_CIRCUMFERENCE;
        ringBar.style.strokeDashoffset = offset;
        window.App.setKpi(ringPct, `${pct}%`);
        ringCount.textContent = `${collected} / ${target}`;
    }

    // ── Slider Binding ───────────────────────────────────────────────────
    if (sliderMaxImages && valMaxImages) {
        sliderMaxImages.addEventListener("input", () => {
            valMaxImages.textContent = sliderMaxImages.value;
            if (!isCollecting) {
                currentTarget = parseInt(sliderMaxImages.value, 10) || 500;
                setRingProgress(0, currentTarget);
            }
        });
        valMaxImages.textContent = sliderMaxImages.value;
    }

    // ── Update Button States ─────────────────────────────────────────────
    function updateControls() {
        const busy = isCollecting || isTraining;
        if (btnStart) btnStart.disabled = busy;
        if (btnStop) btnStop.disabled = !isCollecting;
        if (btnTrainNow) btnTrainNow.disabled = busy;

        const deleteBtns = document.querySelectorAll(".btn-delete-person");
        deleteBtns.forEach((btn) => {
            btn.disabled = busy;
        });
    }

    // ── Preview Control ──────────────────────────────────────────────────
    function setPreviewActive(active) {
        if (!previewImg || !previewEmpty) return;
        if (active) {
            previewImg.src = `/api/collect/feed?t=${Date.now()}`;
            previewImg.style.display = "block";
            previewEmpty.style.display = "none";
        } else {
            previewImg.src = "";
            previewImg.style.display = "none";
            previewEmpty.style.display = "flex";
        }
    }

    // ── Logging helper for Training ──────────────────────────────────────
    function addTrainLog(msg, type = "normal") {
        if (!trainLog) return;
        // Remove empty placeholder
        const empty = trainLog.querySelector(".train-log__entry--dim");
        if (empty) empty.remove();

        const p = document.createElement("p");
        p.className = `train-log__entry ${type ? `train-log__entry--${type}` : ""}`;
        p.textContent = `[${new Date().toLocaleTimeString()}] ${msg}`;
        trainLog.appendChild(p);
        trainLog.scrollTop = trainLog.scrollHeight;
    }

    // ── Persons List ─────────────────────────────────────────────────────
    async function fetchPersons() {
        try {
            const res = await fetch("/api/persons");
            if (!res.ok) throw new Error("Failed to fetch registered persons");
            const list = await res.json();
            renderPersons(list);
        } catch (err) {
            console.error("fetchPersons error:", err);
            if (personsTableBody) {
                personsTableBody.innerHTML = `<tr><td colspan="5" class="table-empty"></td></tr>`;
                window.App.renderState(personsTableBody.querySelector("td"), "error",
                    "Could not load registered people.", fetchPersons);
            }
        }
    }

    function renderPersons(list) {
        persons = Array.isArray(list) ? list : [];
        suggestNextId();
        syncExistingPerson();

        if (!personsTableBody) return;
        if (personsCountBadge) window.App.setKpi(personsCountBadge, persons.length);

        if (!persons || persons.length === 0) {
            personsTableBody.innerHTML = `<tr><td colspan="5" class="table-empty"></td></tr>`;
            window.App.renderState(personsTableBody.querySelector("td"), "empty",
                "No one is registered yet. Fill in the form above to add a person.");
            return;
        }

        personsTableBody.innerHTML = persons
            .map((p) => {
                const initial = (p.name || "U").charAt(0).toUpperCase();
                const isReady = p.image_count >= LOW_IMAGE_THRESHOLD;
                const badgeClass = isReady ? "badge--ready" : "badge--low";
                const badgeLabel = isReady ? "Ready" : `Too few (under ${LOW_IMAGE_THRESHOLD})`;
                const busy = isCollecting || isTraining;

                return `
                <tr>
                    <td data-label="Person">
                        <div class="person-cell">
                            <div class="person-avatar">${window.App.escapeHtml(initial)}</div>
                            <strong>${window.App.escapeHtml(p.name)}</strong>
                        </div>
                    </td>
                    <td data-label="ID" class="col-num"><code>${p.person_id}</code></td>
                    <td data-label="Images" class="col-num">${p.image_count}</td>
                    <td data-label="Status"><span class="badge ${badgeClass}">${badgeLabel}</span></td>
                    <td data-label="Actions" style="text-align:right">
                        <button class="btn btn--ghost btn--sm btn-delete-person" data-id="${p.person_id}" data-name="${window.App.escapeHtml(p.name)}" ${busy ? "disabled" : ""}>
                            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="14" height="14"><polyline points="3 6 5 6 21 6"/><path d="M19 6v14a2 2 0 0 1-2 2H7a2 2 0 0 1-2-2V6m3 0V4a2 2 0 0 1 2-2h4a2 2 0 0 1 2 2v2"/></svg>
                            Delete
                        </button>
                    </td>
                </tr>
            `;
            })
            .join("");


        // Bind delete buttons
        personsTableBody.querySelectorAll(".btn-delete-person").forEach((btn) => {
            btn.addEventListener("click", () => handleDeletePerson(btn.dataset.id, btn.dataset.name));
        });
    }

    async function handleDeletePerson(id, name) {
        const confirmed = await window.App.confirm({
            title: `Delete ${name}?`,
            message: `This removes person #${id} and every face image collected for them, then retrains the model. It cannot be undone.`,
            confirmLabel: "Delete person",
            danger: true,
        });
        if (!confirmed) return;

        try {
            const res = await fetch(`/api/persons/${id}`, { method: "DELETE" });
            const data = await res.json();
            if (!res.ok) {
                window.App.showToast(data.error || "Failed to delete person", "error");
                return;
            }
            window.App.showToast(`${name} deleted. The model is retraining.`, "info");
            fetchPersons();
        } catch (err) {
            window.App.showToast("Network error deleting person", "error");
        }
    }

    // ── Start Collection ─────────────────────────────────────────────────
    if (btnStart) {
        btnStart.addEventListener("click", async () => {
            const errId = document.getElementById("errRegisterPersonId");
            const errName = document.getElementById("errRegisterName");
            if (errId) errId.textContent = "";
            if (errName) errName.textContent = "";
            if (earlyStopNotice) earlyStopNotice.style.display = "none";

            const personId = parseInt(inputPersonId.value, 10);
            const name = inputName.value.trim();
            const maxImages = parseInt(sliderMaxImages.value, 10) || 500;

            let valid = true;
            if (isNaN(personId) || personId < 0) {
                if (errId) errId.textContent = "Person ID must be a non-negative integer";
                valid = false;
            }
            if (!name) {
                if (errName) errName.textContent = "Name is required";
                valid = false;
            } else if (name.length > 50) {
                if (errName) errName.textContent = "Name must be 50 characters or fewer";
                valid = false;
            }

            if (!valid) return;

            currentTarget = maxImages;
            setRingProgress(0, currentTarget);

            try {
                btnStart.disabled = true;
                const res = await fetch("/api/collect/start", {
                    method: "POST",
                    headers: { "Content-Type": "application/json" },
                    body: JSON.stringify({ person_id: personId, name, max_images: maxImages }),
                });
                const data = await res.json();
                if (!res.ok) {
                    window.App.showToast(data.error || "Failed to start collection", "error");
                    btnStart.disabled = false;
                    return;
                }

                window.App.showToast(`Collection started for ${name} (#${personId})`, "info");
                setPreviewActive(true);
            } catch (err) {
                window.App.showToast("Network error starting collection", "error");
                btnStart.disabled = false;
            }
        });
    }

    // ── Stop Collection ──────────────────────────────────────────────────
    if (btnStop) {
        btnStop.addEventListener("click", async () => {
            try {
                btnStop.disabled = true;
                const res = await fetch("/api/collect/stop", { method: "POST" });
                const data = await res.json();
                if (!res.ok) {
                    window.App.showToast(data.error || "Failed to stop collection", "error");
                    return;
                }
                window.App.showToast("Collection stopped early", "info");
                if (earlyStopNotice) earlyStopNotice.style.display = "block";
            } catch (err) {
                window.App.showToast("Network error stopping collection", "error");
            }
        });
    }

    // ── Train Now ────────────────────────────────────────────────────────
    if (btnTrainNow) {
        btnTrainNow.addEventListener("click", async () => {
            try {
                btnTrainNow.disabled = true;
                if (trainSummaryCard) trainSummaryCard.style.display = "none";
                addTrainLog("Training requested", "dim");

                const res = await fetch("/api/train", { method: "POST" });
                const data = await res.json();
                if (!res.ok) {
                    window.App.showToast(data.error || "Failed to start training", "error");
                    btnTrainNow.disabled = false;
                    return;
                }
                window.App.showToast("Training started", "info");
            } catch (err) {
                window.App.showToast("Network error starting training", "error");
                btnTrainNow.disabled = false;
            }
        });
    }

    // ── Socket.IO Event Listeners ────────────────────────────────────────
    const socket = window.App.socket;

    // 1. collect:status
    socket.on("collect:status", (data) => {
        isCollecting = !!data.running;
        updateControls();
        setPreviewActive(isCollecting);

        if (isCollecting) {
            currentTarget = data.target || currentTarget || 500;
            setRingProgress(data.count || 0, currentTarget);
            if (earlyStopNotice) earlyStopNotice.style.display = "none";
        }
    });

    // 2. collect:progress
    socket.on("collect:progress", (data) => {
        currentTarget = data.max_images || currentTarget || 500;
        setRingProgress(data.collected || 0, currentTarget);
    });

    // 3. collect:done
    socket.on("collect:done", (data) => {
        window.App.showToast(`Collection complete for ${data.name}. Training started.`, "success");
        setPreviewActive(false);
        // Ready for the next person: clear the form and suggest a fresh ID
        idTouched = false;
        nameLocked = false;
        inputName.value = "";
        inputName.readOnly = false;
        fetchPersons();
    });

    // 4. collect:error
    socket.on("collect:error", (data) => {
        window.App.showToast(`Collection error: ${data.message}`, "error");
        setPreviewActive(false);
    });

    // 5. train:status
    socket.on("train:status", (data) => {
        isTraining = !!data.running;
        updateControls();

        if (isTraining) {
            if (trainProgressWrap) trainProgressWrap.style.display = "flex";
            const pct = data.pct || 0;
            if (trainProgressBar) trainProgressBar.style.width = `${pct}%`;
            if (trainProgressPct) window.App.setKpi(trainProgressPct, `${pct}%`);
            if (trainProgressMsg) trainProgressMsg.textContent = data.msg || "Training in progress...";
        } else {
            if (trainProgressWrap) trainProgressWrap.style.display = "none";
        }
    });

    // 6. train:progress
    socket.on("train:progress", (data) => {
        if (data.step === "scanning") {
            addTrainLog(`Scanning ${data.total} image(s)...`, "dim");
        } else if (data.step === "loading") {
            if (data.current % 20 === 0 || data.current === data.total) {
                addTrainLog(`Loaded ${data.current}/${data.total} images...`);
            }
        } else if (data.step === "training") {
            addTrainLog(`Training LBPH on ${data.total_images} faces...`);
        }
    });

    // 7. train:done
    socket.on("train:done", (data) => {
        window.App.showToast("Training complete", "success");
        addTrainLog(`Training finished successfully: ${data.total_images} images processed.`, "success");

        if (trainSummaryCard) {
            trainSummaryCard.style.display = "flex";
            if (trainSumTotal) window.App.setKpi(trainSumTotal, data.total_images || 0);
            if (trainSumPersons) window.App.setKpi(trainSumPersons, (data.persons || []).length);
            if (trainSumSkipped) window.App.setKpi(trainSumSkipped, data.bad_skipped || 0);

            if (trainSumList) {
                trainSumList.innerHTML = (data.persons || [])
                    .map((p) => `<span class="train-person-pill">${window.App.escapeHtml(String(p.name))}: ${p.count} images</span>`)
                    .join("");
            }
        }

        fetchPersons();
    });

    // 8. train:error
    socket.on("train:error", (data) => {
        window.App.showToast(`Training error: ${data.message}`, "error");
        addTrainLog(`Error: ${data.message}`, "error");
    });

    // ── Tab Navigation listener (refresh list when opening tab) ──────────
    window.addEventListener("tab:navigate", (e) => {
        if (e.detail && e.detail.tab === "register") {
            fetchPersons();
        }
    });

    // Initial load
    fetchPersons();
})();
