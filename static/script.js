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
  const scanText = document.getElementById("scanText");
  const scanStatus = document.getElementById("scanStatus");
  const downloadBar = document.getElementById("downloadBar");
  const downloadBtn = document.getElementById("downloadBtn");
  const doneMsg = document.getElementById("doneMsg");

  let pollTimer = null;

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

      if (data.status === "complete") {
        clearInterval(pollTimer);
        scanStatus.hidden = true;
        runBtn.disabled = false;
        runBtn.textContent = "Run Traceline";

        doneMsg.textContent = `Done — ${data.total} companies processed.`;
        downloadBtn.href = `/api/download/${jobId}`;
        downloadBtn.classList.remove("btn-disabled");
        downloadBtn.removeAttribute("tabindex");
      } else if (data.current) {
        scanText.innerHTML = `Checking <span class="company-name">${escapeHtml(data.current)}</span>…`;
      }
    }, 900);
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
    scanStatus.hidden = false;
    scanText.textContent = "Starting…";
    progressFill.style.width = "0%";
    doneMsg.textContent = "Download unlocks once every company has been checked.";
    downloadBtn.textContent = "Download CSV ↓";
    downloadBtn.classList.add("btn-disabled");
    downloadBtn.setAttribute("tabindex", "-1");

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
      poll(data.job_id);
    } catch (err) {
      errorMsg.textContent = "Could not reach the server.";
      runBtn.disabled = false;
      runBtn.textContent = "Run Traceline";
    }
  });
});
