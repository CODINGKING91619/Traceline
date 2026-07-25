document.addEventListener("DOMContentLoaded", () => {
  const tabBtns = document.querySelectorAll(".tab-btn");
  const tabPanels = document.querySelectorAll(".tab-panel");
  tabBtns.forEach(btn => {
    btn.addEventListener("click", () => {
      tabBtns.forEach(b => b.classList.remove("active"));
      tabPanels.forEach(p => p.classList.remove("active"));
      btn.classList.add("active");
      document.querySelector(`.tab-panel[data-panel="${btn.dataset.tab}"]`).classList.add("active");
    });
  });

  const fileInput = document.getElementById("fileInput");
  const fileDrop = document.getElementById("fileDrop");
  const fileLabel = document.getElementById("fileLabel");
  fileInput.addEventListener("change", () => {
    if (fileInput.files.length) fileLabel.textContent = fileInput.files[0].name;
  });
  ["dragover", "dragleave", "drop"].forEach(evt => {
    fileDrop.addEventListener(evt, e => e.preventDefault());
  });
  fileDrop.addEventListener("drop", e => {
    if (e.dataTransfer.files.length) {
      fileInput.files = e.dataTransfer.files;
      fileLabel.textContent = e.dataTransfer.files[0].name;
    }
  });

  const runBtn = document.getElementById("runBtn");
  const errorMsg = document.getElementById("errorMsg");
  const resultsSection = document.getElementById("resultsSection");
  const progressFill = document.getElementById("progressFill");
  const progressLabel = document.getElementById("progressLabel");
  const resultsBody = document.getElementById("resultsBody");
  const downloadBar = document.getElementById("downloadBar");
  const downloadBtn = document.getElementById("downloadBtn");

  let pollTimer = null;

  function statusClass(status) {
    if (status === "found") return "status-found";
    if (status === "error") return "status-error";
    return "status-nodata";
  }

  function renderRows(rows) {
    resultsBody.innerHTML = "";
    rows.forEach(r => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td>${escapeHtml(r.company)}</td>
        <td>${escapeHtml(r.website)}</td>
        <td>${escapeHtml(r.emails) || "—"}</td>
        <td>${escapeHtml(r.phones) || "—"}</td>
        <td>${escapeHtml(r.instagram) || "—"}</td>
        <td class="${statusClass(r.status)}">${r.status}</td>
      `;
      resultsBody.appendChild(tr);
    });
  }

  function escapeHtml(str) {
    if (!str) return "";
    return str.replace(/[&<>"']/g, m => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;"
    }[m]));
  }

  function poll(jobId) {
    pollTimer = setInterval(async () => {
      const res = await fetch(`/api/status/${jobId}`);
      if (!res.ok) { clearInterval(pollTimer); return; }
      const data = await res.json();
      const pct = data.total ? Math.round((data.done / data.total) * 100) : 0;
      progressFill.style.width = pct + "%";
      progressLabel.textContent = `${data.done} / ${data.total} processed`;
      renderRows(data.rows);

      if (data.status === "complete") {
        clearInterval(pollTimer);
        progressLabel.textContent = `Done — ${data.total} / ${data.total} processed`;
        downloadBar.hidden = false;
        downloadBtn.href = `/api/download/${jobId}`;
        runBtn.disabled = false;
        runBtn.textContent = "Run Traceline";
      }
    }, 1200);
  }

  runBtn.addEventListener("click", async () => {
    errorMsg.textContent = "";
    const activeTab = document.querySelector(".tab-btn.active").dataset.tab;
    const formData = new FormData();

    if (activeTab === "upload") {
      if (!fileInput.files.length) {
        errorMsg.textContent = "Choose a CSV file first.";
        return;
      }
      formData.append("file", fileInput.files[0]);
    } else {
      const text = document.getElementById("pasteArea").value.trim();
      if (!text) {
        errorMsg.textContent = "Paste at least one company first.";
        return;
      }
      formData.append("pasted_text", text);
    }

    runBtn.disabled = true;
    runBtn.textContent = "Starting…";
    downloadBar.hidden = true;
    resultsBody.innerHTML = "";
    progressFill.style.width = "0%";

    try {
      const res = await fetch("/api/start", { method: "POST", body: formData });
      const data = await res.json();
      if (!res.ok) {
        errorMsg.textContent = data.error || "Something went wrong.";
        runBtn.disabled = false;
        runBtn.textContent = "Run Traceline";
        return;
      }
      resultsSection.hidden = false;
      resultsSection.scrollIntoView({ behavior: "smooth" });
      runBtn.textContent = "Running…";
      progressLabel.textContent = `0 / ${data.total} processed`;
      poll(data.job_id);
    } catch (err) {
      errorMsg.textContent = "Could not reach the server.";
      runBtn.disabled = false;
      runBtn.textContent = "Run Traceline";
    }
  });
});
