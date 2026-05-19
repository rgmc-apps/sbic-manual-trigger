(function () {
  "use strict";

  const form        = document.getElementById("trigger-form");
  const submitBtn   = document.getElementById("submit-btn");
  const formCard    = document.getElementById("form-card");
  const progressCard = document.getElementById("progress-card");
  const progressBar  = document.getElementById("progress-bar");
  const progressLabel = document.getElementById("progress-label");
  const resultCard  = document.getElementById("result-card");
  const resultTitle = document.getElementById("result-title");
  const resultMeta  = document.getElementById("result-meta");
  const resultIcon  = document.getElementById("result-icon");
  const resultBody  = document.getElementById("result-body");
  const errorNotice = document.getElementById("error-notice");
  const resetBtn    = document.getElementById("reset-btn");

  // ── Fake progress ticker ──────────────────────────────────────────────────
  let progressInterval = null;
  let currentWidth = 0;

  function startProgress() {
    currentWidth = 0;
    progressBar.style.width = "0%";

    const steps = [
      { target: 15, label: "Connecting to endpoint…",    delay: 600  },
      { target: 35, label: "Request accepted…",           delay: 2000 },
      { target: 60, label: "Processing data…",            delay: 5000 },
      { target: 80, label: "Finalizing upload…",          delay: 10000 },
      { target: 92, label: "Almost done…",                delay: 20000 },
    ];

    let stepIndex = 0;

    function tick() {
      if (stepIndex < steps.length) {
        const step = steps[stepIndex++];
        animateTo(step.target, step.label);
        progressInterval = setTimeout(tick, step.delay);
      }
    }
    tick();
  }

  function animateTo(target, label) {
    if (label) progressLabel.textContent = label;
    const duration = 800;
    const start = currentWidth;
    const diff = target - start;
    const startTime = performance.now();

    function frame(now) {
      const elapsed = now - startTime;
      const progress = Math.min(elapsed / duration, 1);
      currentWidth = start + diff * easeOut(progress);
      progressBar.style.width = currentWidth + "%";
      if (progress < 1) requestAnimationFrame(frame);
    }
    requestAnimationFrame(frame);
  }

  function finishProgress(label) {
    clearTimeout(progressInterval);
    progressLabel.textContent = label || "Done";
    animateTo(100);
  }

  function easeOut(t) { return 1 - Math.pow(1 - t, 3); }

  // ── Validation ────────────────────────────────────────────────────────────
  function validate() {
    let valid = true;

    const fields = [
      { id: "name",       errId: "name-error",       msg: "Full name is required." },
      { id: "department", errId: "department-error",  msg: "Department is required." },
      { id: "process",    errId: "process-error",     msg: "Please select a process." },
    ];

    fields.forEach(({ id, errId, msg }) => {
      const el  = document.getElementById(id);
      const err = document.getElementById(errId);
      if (!el.value.trim()) {
        el.classList.add("invalid");
        err.textContent = msg;
        valid = false;
      } else {
        el.classList.remove("invalid");
        err.textContent = "";
      }
    });

    return valid;
  }

  // Clear validation state on input
  ["name", "department", "process"].forEach((id) => {
    document.getElementById(id).addEventListener("input", function () {
      this.classList.remove("invalid");
      document.getElementById(id + "-error").textContent = "";
    });
  });

  // ── Show / hide panels ────────────────────────────────────────────────────
  function showProgress() {
    formCard.classList.add("hidden");
    resultCard.classList.add("hidden");
    progressCard.classList.remove("hidden");
  }

  function showResult(data) {
    progressCard.classList.add("hidden");

    const ok = data.ok === true;

    // Icon
    resultIcon.className = "result-icon " + (ok ? "success" : "error");
    resultIcon.textContent = ok ? "✓" : "✗";

    // Title & meta
    resultTitle.textContent = ok
      ? `${data.process_name} completed successfully`
      : `${data.process_name} encountered an error`;
    resultTitle.style.color = ok ? "var(--success)" : "var(--error)";

    resultMeta.textContent = `HTTP ${data.status_code ?? "—"}  ·  ${data.timestamp ?? ""}`;

    // Body
    const body = data.response ?? data.error ?? "No response body.";
    resultBody.textContent = typeof body === "object"
      ? JSON.stringify(body, null, 2)
      : String(body);

    // Error notice
    if (ok) {
      errorNotice.classList.add("hidden");
    } else {
      errorNotice.classList.remove("hidden");
    }

    resultCard.classList.remove("hidden");
    resultCard.className = "card card-result " + (ok ? "success" : "error");

    resultCard.scrollIntoView({ behavior: "smooth", block: "start" });
  }

  function showError(message) {
    showResult({
      ok: false,
      status_code: "—",
      process_name: "Request",
      timestamp: new Date().toLocaleString(),
      error: message,
    });
  }

  // ── Reset ─────────────────────────────────────────────────────────────────
  resetBtn.addEventListener("click", function () {
    resultCard.classList.add("hidden");
    formCard.classList.remove("hidden");
    submitBtn.disabled = false;
    form.reset();
    window.scrollTo({ top: 0, behavior: "smooth" });
  });

  // ── Submit ────────────────────────────────────────────────────────────────
  form.addEventListener("submit", async function (e) {
    e.preventDefault();

    if (!validate()) return;

    const payload = {
      name:       document.getElementById("name").value.trim(),
      department: document.getElementById("department").value.trim(),
      process:    document.getElementById("process").value.trim(),
    };

    submitBtn.disabled = true;
    showProgress();
    startProgress();

    try {
      const res = await fetch("/trigger", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });

      const data = await res.json();
      finishProgress(data.ok ? "Completed!" : "Finished with errors");

      // Small delay so the bar visually completes before switching panels
      setTimeout(() => showResult(data), 500);
    } catch (err) {
      finishProgress("Network error");
      setTimeout(() => showError("Network error: " + err.message), 500);
    }
  });
})();
