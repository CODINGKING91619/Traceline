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
from pathlib import Path

from flask import Flask, request, jsonify, render_template, send_file, abort

from extractor import extract_contacts

app = Flask(__name__)

JOBS = {}  # job_id -> dict(status, total, done, rows, error)
JOBS_LOCK = threading.Lock()

MAX_COMPANIES = 500


def parse_input_text(raw_text):
    """Accepts pasted lines like 'Company Name, https://site.com' (comma
    or tab separated). Skips a header row if one is detected."""
    companies = []
    for line in raw_text.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = re.split(r"\t|,", line, maxsplit=1)
        if len(parts) != 2:
            continue
        name, site = parts[0].strip(), parts[1].strip()
        if name.lower() in ("company_name", "company", "name") and "http" not in site.lower():
            continue  # header row
        if name and site:
            companies.append((name, site))
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
        if name and site:
            companies.append((name, site))
    return companies


def run_job(job_id, companies):
    job = JOBS[job_id]
    for name, site in companies:
        try:
            emails, phones, instagram = extract_contacts(site)
            status = "found" if (emails or phones or instagram) else "no data"
        except Exception as e:
            emails, phones, instagram = [], [], None
            status = f"error"

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
        job["status"] = "complete"


@app.route("/")
def index():
    return render_template("index.html", max_companies=MAX_COMPANIES)


@app.route("/api/start", methods=["POST"])
def start_job():
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
        JOBS[job_id] = {"status": "running", "total": len(companies), "done": 0, "rows": []}

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
            "rows": job["rows"][-50:],  # latest rows for live preview
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
    return send_file(
        mem,
        mimetype="text/csv",
        as_attachment=True,
        download_name=f"contacts_{job_id}.csv",
    )


if __name__ == "__main__":
    app.run(debug=True, port=5000)
