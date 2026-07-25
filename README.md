# Traceline

Self-serve web app: a customer pastes or uploads a list of companies
(name + website), Traceline scrapes each site for email, phone, and
Instagram, shows results live, and lets them download a CSV.

## Run locally

```
pip install -r requirements.txt
python app.py
```

Open http://localhost:5000

## Deploy so a customer can actually use it

This is a Flask app — it needs a real server, not a static host. Cheapest
options that work with zero config changes:

- **Railway** or **Render** (free/cheap tier, connect your GitHub repo, it
  auto-detects Flask and runs `python app.py`)
- **PythonAnywhere** (has a free tier, good for a single always-on demo link)
- Your own VPS with `gunicorn app:app` behind nginx, if you want it production-grade

Whichever you pick, change the last line of `app.py` before going live:

```python
app.run(debug=False, port=5000)
```

(already set to `debug=False`) and run it behind `gunicorn` in production
instead of Flask's dev server, e.g.:

```
gunicorn -w 4 -b 0.0.0.0:8000 app:app
```

## Selling it

Since jobs run in-memory per server process, this is fine for you running
one instance and sending clients the link for their one-time run. If you
want multiple customers running jobs at the same time without stepping on
each other, it already handles that (each run gets its own job_id) — the
one thing it does NOT have is login/accounts, so anyone with the link can
start a run. For a one-time-sale-per-project model that's usually fine
(you send the link, they use it, you take it down after).

## Known limits (be upfront with buyers)

- Instagram is only found if the company's own website links to it —
  Traceline does not search Instagram directly (that's against their ToS
  and gets IPs blocked fast)
- Sites that block scrapers/bots or require JavaScript to render contact
  info won't yield results
- Max 500 companies per run (edit `MAX_COMPANIES` in `app.py` to change)
