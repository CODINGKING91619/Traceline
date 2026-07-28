"""
Contact Ledger — backend
-------------------------
Self-serve web app: a user pastes or uploads a list of companies
(name + website), the server scrapes each site for email / phone /
Instagram in a background thread, the frontend polls for progress,
and the finished results are downloadable as a CSV.

Run:
    pip install -r requirements.txt
    python app.py
Then open http://localhost:5000
"""

import csv
import io
import re
import threading
import time
import uuid
from urllib.parse import urlparse

from flask import Flask, request, jsonify, render_template, send_file, abort

from extractor import extract_contacts

app = Flask(__name__)

JOBS = {}  # job_id -> dict(status, total, done, rows, current)
JOBS_LOCK = threading.Lock()

MAX_COMPANIES = 300
STALE_JOB_MAX_AGE_SEC = 3 * 60 * 60  # 3 hours


def _cleanup_stale_jobs():
    """Removes old finished jobs that were never downloaded, so an abandoned
    run (closed tab, failed run, etc.) doesn't sit in server memory forever.
    Called opportunistically whenever a new run starts."""
    now = time.time()
    with JOBS_LOCK:
        stale_ids = [
            jid for jid, job in JOBS.items()
            if job.get("status") == "complete" and now - job.get("created_at", now) > STALE_JOB_MAX_AGE_SEC
        ]
        for jid in stale_ids:
            JOBS.pop(jid, None)


PHONE_LINE_RE = re.compile(r"^\+?[\d\s\-().]{7,}$")
URL_HINT_RE = re.compile(r"https?://|www\.", re.IGNORECASE)


def parse_input_text(raw_text):
    """Handles company list inputs:
    1) Standard CSV/TSV line per company: 'Company Name, https://site.com'
    2) Multi-line blocks separated by blank lines
    Returns a list of dicts: {"name": ..., "website": ..., "phone": ... or None}
    """
    lines = raw_text.splitlines()
    companies = []

    for line in lines:
        line = line.strip()
        if not line:
            continue

        parts = [p.strip() for p in re.split(r"\t|,", line) if p.strip()]
        if len(parts) >= 2:
            name, site = parts[0], parts[1]
            phone = parts[2] if len(parts) > 2 and PHONE_LINE_RE.match(parts[2]) else None
            
            if name.lower() in ("company_name", "company name", "company", "name") and "http" not in site.lower():
                continue
            if URL_HINT_RE.search(site):
                companies.append({"name": name, "website": site, "phone": phone})
                continue
            elif URL_HINT_RE.search(name):
                companies.append({"name": site, "website": name, "phone": phone})
                continue

        if URL_HINT_RE.search(line):
            domain = urlparse(line if line.startswith("http") else "https://" + line).netloc or line
            companies.append({"name": domain, "website": line, "phone": None})

    return companies


def parse_input_csv(file_storage):
    content = file_storage.read().decode("utf-8", errors="ignore")
    companies = []
    reader = csv.reader(io.StringIO(content))
    rows = list(reader)
    if not rows:
        return companies
    start = 0
    header = [c.strip().lower() for c in rows[0]]
    if "website" in header or "company" in header[0]:
        start = 1
    for row in rows[start:]:
        if len(row) < 2:
            continue
        name, site = row[0].strip(), row[1].strip()
        phone = row[2].strip() if len(row) > 2 and row[2].strip() else None
        if name and site:
            companies.append({"name": name, "website": site, "phone": phone})
    return companies


def run_job(job_id, companies):
    job = JOBS[job_id]
    for company in companies:
        name, site, given_phone = company["name"], company["website"], company.get("phone")

        with JOBS_LOCK:
            job["current"] = name

        try:
            emails, phones, instagram = extract_contacts(site)
            if not phones and given_phone:
                phones = [given_phone]
            status = "found" if (emails or phones or instagram) else "no data"
        except Exception:
            emails, instagram = [], None
            phones = [given_phone] if given_phone else []
            status = "error"

        with JOBS_LOCK:
            job["rows"].append({
                "company": name,
                "website": site,
                "emails": ", ".join(emails),
                "phones": ", ".join(phones),
                "instagram": instagram or "",
                "status": status,
            })
            job["done"] += 1

    with JOBS_LOCK:
        job["current"] = None
        job["status"] = "complete"


@app.route("/")
def index():
    return render_template("index.html", max_companies=MAX_COMPANIES)


@app.route("/api/start", methods=["POST"])
def start_job():
    _cleanup_stale_jobs()

    companies = []

    if "file" in request.files and request.files["file"].filename:
        companies = parse_input_csv(request.files["file"])
    else:
        raw_text = request.form.get("pasted_text", "")
        companies = parse_input_text(raw_text)

    if not companies:
        return jsonify({"error": "No valid company rows found. Each line needs a name and a website."}), 400

    if len(companies) > MAX_COMPANIES:
        return jsonify({"error": f"Max {MAX_COMPANIES} companies per run. You submitted {len(companies)}."}), 400

    job_id = uuid.uuid4().hex[:12]
    with JOBS_LOCK:
        JOBS[job_id] = {
            "status": "running", "total": len(companies), "done": 0,
            "rows": [], "current": None, "created_at": time.time(),
        }

    thread = threading.Thread(target=run_job, args=(job_id, companies), daemon=True)
    thread.start()

    return jsonify({"job_id": job_id, "total": len(companies)})


@app.route("/api/status/<job_id>")
def job_status(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        return jsonify({
            "status": job["status"],
            "total": job["total"],
            "done": job["done"],
            "current": job.get("current"),
        })


@app.route("/api/download/<job_id>")
def download(job_id):
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            abort(404)
        rows = list(job["rows"])

    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(["Company Name", "Website", "Emails", "Phones", "Instagram", "Status"])
    for r in rows:
        writer.writerow([r["company"], r["website"], r["emails"], r["phones"], r["instagram"], r["status"]])

    mem = io.BytesIO(buf.getvalue().encode("utf-8"))

    # Free the job's data from server memory now that it's safely copied into
    # the response above -- without this, a long-running server would slowly
    # accumulate every past run's results in memory forever.
    with JOBS_LOCK:
        JOBS.pop(job_id, None)

    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"contacts_{job_id}.csv",
    )


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(debug=False, host="0.0.0.0", port=port)
