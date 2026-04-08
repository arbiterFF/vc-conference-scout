#!/usr/bin/env python3
"""
VC Scout - multi-scan HTTP server.
Serves the SPA, manages scans (create, list, get, update, delete companies, star).
Each scan lives in scans/<id>/ with config.json, results.json, starred.json, progress.json.

Usage: python3 server.py
"""

import json
import os
import re
import subprocess
import sys
from datetime import datetime, timezone
from http.server import HTTPServer, SimpleHTTPRequestHandler
from urllib.parse import urlparse

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
SCANS_DIR = os.path.join(SCRIPT_DIR, "scans")
os.makedirs(SCANS_DIR, exist_ok=True)


# ---------- Helpers ----------

def slugify(s):
    s = re.sub(r"[^\w\s-]", "", s.lower()).strip()
    s = re.sub(r"[-\s]+", "-", s)
    return s or "scan"


def scan_dir(scan_id):
    return os.path.join(SCANS_DIR, scan_id)


def load_json(path, default=None):
    if not os.path.exists(path):
        return default
    with open(path) as f:
        return json.load(f)


def save_json(path, data):
    with open(path, "w") as f:
        json.dump(data, f, indent=2)


def list_scans():
    scans = []
    if not os.path.exists(SCANS_DIR):
        return scans
    for entry in sorted(os.listdir(SCANS_DIR)):
        if entry.startswith("."):
            continue
        d = scan_dir(entry)
        if not os.path.isdir(d):
            continue
        config = load_json(os.path.join(d, "config.json"), {}) or {}
        progress = load_json(os.path.join(d, "progress.json"), {}) or {}
        results = load_json(os.path.join(d, "results.json"), {}) or {}
        # New pipeline writes "triaged" / "enriched"; old runs wrote "relevant_matches".
        # Fall back through both, then to the actual results array length.
        match_count = (
            results.get("triaged")
            or results.get("relevant_matches")
            or len(results.get("results") or [])
        )
        scans.append({
            "id": entry,
            "name": config.get("name", entry),
            "url": config.get("url", ""),
            "synopsis": config.get("synopsis", ""),
            "created_at": config.get("created_at", ""),
            "completed_at": config.get("completed_at"),
            "stage": progress.get("stage", "unknown"),
            "stage_label": progress.get("stage_label", ""),
            "percent": progress.get("percent", 0),
            "total_scraped": results.get("total_scraped", 0),
            "relevant_matches": match_count,
            "enriched": results.get("enriched", 0),
        })
    # Most recent first
    scans.sort(key=lambda s: s.get("created_at", ""), reverse=True)
    return scans


# ---------- HTTP Handler ----------

class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=SCRIPT_DIR, **kwargs)

    def log_message(self, format, *args):
        # Quieter logs
        sys.stderr.write("[%s] %s\n" % (self.log_date_time_string(), format % args))

    # ---- Routing ----

    def do_GET(self):
        path = urlparse(self.path).path

        if path.startswith("/api/"):
            self.handle_api_get(path)
            return

        # Default: serve static files
        super().do_GET()

    def do_POST(self):
        path = urlparse(self.path).path
        length = int(self.headers.get("Content-Length", 0))
        body_bytes = self.rfile.read(length)
        try:
            body = json.loads(body_bytes) if body_bytes else {}
        except Exception:
            body = {}

        if path.startswith("/api/"):
            self.handle_api_post(path, body)
            return

        self._json_err(404, "Not found")

    def do_DELETE(self):
        path = urlparse(self.path).path
        if path.startswith("/api/scans/"):
            scan_id = path[len("/api/scans/"):]
            d = scan_dir(scan_id)
            if not os.path.isdir(d):
                self._json_err(404, "Scan not found")
                return
            # Soft-delete: move to scans/.trash/<id>-<timestamp>/
            trash_dir = os.path.join(SCANS_DIR, ".trash")
            os.makedirs(trash_dir, exist_ok=True)
            ts = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S")
            dest = os.path.join(trash_dir, f"{scan_id}-{ts}")
            os.rename(d, dest)
            self._json_ok()
            return
        self._json_err(404, "Not found")

    # ---- API GET ----

    def handle_api_get(self, path):
        if path == "/api/scans":
            self._json(list_scans())
            return

        m = re.match(r"^/api/scans/([^/]+)$", path)
        if m:
            scan_id = m.group(1)
            d = scan_dir(scan_id)
            if not os.path.isdir(d):
                self._json_err(404, "Scan not found")
                return
            config = load_json(os.path.join(d, "config.json"), {}) or {}
            results = load_json(os.path.join(d, "results.json"), {}) or {"results": []}
            starred = load_json(os.path.join(d, "starred.json"), {}) or {}
            progress = load_json(os.path.join(d, "progress.json"), {}) or {}
            # Strip api_key from response
            config = {k: v for k, v in config.items() if k != "api_key"}
            self._json({
                "id": scan_id,
                "config": config,
                "results": results,
                "starred": starred,
                "progress": progress,
            })
            return

        m = re.match(r"^/api/scans/([^/]+)/progress$", path)
        if m:
            scan_id = m.group(1)
            d = scan_dir(scan_id)
            if not os.path.isdir(d):
                self._json_err(404, "Scan not found")
                return
            progress = load_json(os.path.join(d, "progress.json"), {}) or {}
            self._json(progress)
            return

        self._json_err(404, "Not found")

    # ---- API POST ----

    def handle_api_post(self, path, body):
        # Extract profile from a VC firm website
        if path == "/api/extract-profile":
            self.extract_profile(body)
            return

        # Create new scan
        if path == "/api/scans":
            self.create_scan(body)
            return

        m = re.match(r"^/api/scans/([^/]+)/(pause|resume|cancel)$", path)
        if m:
            self.control_scan(m.group(1), m.group(2))
            return

        m = re.match(r"^/api/scans/([^/]+)/enrich-uncertain$", path)
        if m:
            self.enrich_uncertain(m.group(1), body)
            return

        m = re.match(r"^/api/scans/([^/]+)/star$", path)
        if m:
            scan_id = m.group(1)
            d = scan_dir(scan_id)
            if not os.path.isdir(d):
                self._json_err(404, "Scan not found")
                return
            save_json(os.path.join(d, "starred.json"), body)
            self._json_ok()
            return

        m = re.match(r"^/api/scans/([^/]+)/update-company$", path)
        if m:
            scan_id = m.group(1)
            d = scan_dir(scan_id)
            results_path = os.path.join(d, "results.json")
            data = load_json(results_path)
            if not data:
                self._json_err(404, "Scan not found")
                return
            name = body.get("_originalName") or body.get("name")
            for i, r in enumerate(data["results"]):
                if r["name"] == name:
                    for k, v in body.items():
                        if not k.startswith("_"):
                            data["results"][i][k] = v
                    save_json(results_path, data)
                    self._json_ok()
                    return
            self._json_err(404, "Company not found")
            return

        m = re.match(r"^/api/scans/([^/]+)/delete-company$", path)
        if m:
            scan_id = m.group(1)
            d = scan_dir(scan_id)
            results_path = os.path.join(d, "results.json")
            data = load_json(results_path)
            if not data:
                self._json_err(404, "Scan not found")
                return
            name = body.get("name")
            before = len(data["results"])
            data["results"] = [r for r in data["results"] if r["name"] != name]
            if len(data["results"]) == before:
                self._json_err(404, "Company not found")
                return
            data["relevant_matches"] = len(data["results"])
            save_json(results_path, data)
            self._json_ok()
            return

        self._json_err(404, "Not found")

    # ---- Scan control ----

    def control_scan(self, scan_id, action):
        import signal
        d = scan_dir(scan_id)
        if not os.path.isdir(d):
            self._json_err(404, "Scan not found")
            return
        pid_path = os.path.join(d, "scan.pid")
        if not os.path.exists(pid_path):
            self._json_err(400, "No active scan PID")
            return
        try:
            pid = int(open(pid_path).read().strip())
        except Exception:
            self._json_err(400, "Invalid PID file")
            return

        # Check process is alive
        try:
            os.kill(pid, 0)
        except OSError:
            self._json_err(400, "Scan process is no longer running")
            return

        try:
            if action == "pause":
                os.kill(pid, signal.SIGSTOP)
                open(os.path.join(d, "paused.flag"), "w").close()
                # Mark in progress.json
                p = load_json(os.path.join(d, "progress.json"), {}) or {}
                p["paused"] = True
                save_json(os.path.join(d, "progress.json"), p)
            elif action == "resume":
                os.kill(pid, signal.SIGCONT)
                try:
                    os.remove(os.path.join(d, "paused.flag"))
                except FileNotFoundError:
                    pass
                p = load_json(os.path.join(d, "progress.json"), {}) or {}
                p["paused"] = False
                save_json(os.path.join(d, "progress.json"), p)
            elif action == "cancel":
                # SIGCONT first in case it was paused, then SIGTERM
                try: os.kill(pid, signal.SIGCONT)
                except OSError: pass
                os.kill(pid, signal.SIGTERM)
                try:
                    os.remove(os.path.join(d, "paused.flag"))
                except FileNotFoundError:
                    pass
                p = load_json(os.path.join(d, "progress.json"), {}) or {}
                p["stage"] = "cancelled"
                p["stage_label"] = "Cancelled by user"
                p["paused"] = False
                save_json(os.path.join(d, "progress.json"), p)
            self._json_ok()
        except Exception as e:
            self._json_err(500, str(e))

    # ---- Enrich uncertain (post-scan deepening) ----

    def enrich_uncertain(self, scan_id, body):
        api_key = body.get("api_key", "").strip()
        if not api_key:
            self._json_err(400, "Missing api_key")
            return
        d = scan_dir(scan_id)
        if not os.path.isdir(d):
            self._json_err(404, "Scan not found")
            return
        config_path = os.path.join(d, "config.json")
        config = load_json(config_path) or {}
        # Re-attach the api_key for the duration of the run; scout.py strips it after
        config["api_key"] = api_key
        save_json(config_path, config)

        save_json(os.path.join(d, "progress.json"), {
            "stage": "enriching",
            "stage_label": "Starting deep enrichment...",
            "percent": 0,
        })

        log_file = open(os.path.join(d, "scout.log"), "a")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(SCRIPT_DIR, "scout.py"), d, "--enrich-uncertain"],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=SCRIPT_DIR,
        )
        with open(os.path.join(d, "scan.pid"), "w") as f:
            f.write(str(proc.pid))

        self._json_ok()

    # ---- Profile extraction ----

    def extract_profile(self, body):
        url = body.get("url", "").strip()
        api_key = body.get("api_key", "").strip()
        if not url or not api_key:
            self._json_err(400, "Missing url or api_key")
            return

        try:
            import requests
            from bs4 import BeautifulSoup

            resp = requests.get(url, timeout=20, headers={
                "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36"
            })
            resp.raise_for_status()
            soup = BeautifulSoup(resp.text, "html.parser")
            for tag in soup(["script", "style", "nav", "footer"]):
                tag.decompose()
            page_text = soup.get_text(separator="\n", strip=True)[:8000]

            prompt = f"""Below is the text content of a VC firm's website. Extract their investment thesis and profile.

Write a concise but specific profile (4-10 lines) covering:
- Firm name and stage focus (pre-seed, seed, Series A, etc.)
- Sectors / themes they invest in
- Geographic focus (if specified)
- Their distinctive thesis or angle
- Specific sub-areas or types of companies they look for (as bullet points)

Write in second-person addressing the user (e.g. "You invest in..."). Be specific. Skip team bios, news, portfolio company names, contact info.

WEBSITE TEXT:
{page_text}

Respond with just the profile text, no preamble."""

            api_resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "content-type": "application/json",
                    "anthropic-version": "2023-06-01",
                },
                json={
                    "model": "claude-sonnet-4-5-20250929",
                    "max_tokens": 1024,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=60,
            )
            api_resp.raise_for_status()
            profile = api_resp.json()["content"][0]["text"].strip()
            self._json({"profile": profile})
        except Exception as e:
            self._json_err(500, str(e))

    # ---- Scan creation ----

    def create_scan(self, body):
        required = ["name", "profile", "categories", "api_key"]
        for k in required:
            if not body.get(k):
                self._json_err(400, f"Missing field: {k}")
                return
        # Either url or companies must be provided
        if not body.get("url") and not body.get("companies"):
            self._json_err(400, "Provide either url or companies")
            return

        # Generate unique ID from name
        base_id = slugify(body["name"])
        scan_id = base_id
        counter = 2
        while os.path.exists(scan_dir(scan_id)):
            scan_id = f"{base_id}-{counter}"
            counter += 1

        d = scan_dir(scan_id)
        os.makedirs(d, exist_ok=True)

        config = {
            "name": body["name"],
            "url": body.get("url") or "",
            "profile": body["profile"],
            "categories": body["categories"],
            "auto_categorize": bool(body.get("auto_categorize")),
            "selector": body.get("selector") or None,
            "source": "csv" if body.get("companies") else "url",
            "created_at": datetime.now(timezone.utc).isoformat(),
            "api_key": body["api_key"],  # stored only until scan completes; scout.py strips it
        }
        save_json(os.path.join(d, "config.json"), config)

        # If user uploaded companies directly, save them so scout.py skips scraping
        if body.get("companies"):
            save_json(os.path.join(d, "companies.json"), body["companies"])

        # Initial progress
        save_json(os.path.join(d, "progress.json"), {
            "stage": "starting",
            "stage_label": "Starting scan...",
            "percent": 0,
        })

        # Spawn scout subprocess (detached) and record PID
        log_file = open(os.path.join(d, "scout.log"), "w")
        proc = subprocess.Popen(
            [sys.executable, os.path.join(SCRIPT_DIR, "scout.py"), d],
            stdout=log_file,
            stderr=subprocess.STDOUT,
            cwd=SCRIPT_DIR,
        )
        with open(os.path.join(d, "scan.pid"), "w") as f:
            f.write(str(proc.pid))

        self._json({"id": scan_id})

    # ---- Helpers ----

    def _json(self, data, code=200):
        body = json.dumps(data).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json_ok(self):
        self._json({"ok": True})

    def _json_err(self, code, msg):
        self._json({"error": msg}, code=code)


if __name__ == "__main__":
    port = 8081
    server = HTTPServer(("", port), Handler)
    print(f"VC Scout serving at http://localhost:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
