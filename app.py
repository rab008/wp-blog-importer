"""WordPress Blog Importer — local Flask web app.

Run:
    python3 app.py [--port 5050] [--no-browser]

Opens a browser to http://127.0.0.1:5050 with a form to import .docx/.md
blog posts as drafts to any WordPress site.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import secrets
import subprocess
import sys
import threading
import time
import webbrowser
from dataclasses import asdict
from pathlib import Path

from flask import (
    Flask,
    Response,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    session,
    stream_with_context,
    url_for,
)

import importer
from importer import (
    ScheduleConfig,
    SiteConfig,
    build_parsed_posts,
    check_credentials,
    detect_seo_plugin,
    detect_seo_plugin_installed,
    list_source_files,
    load_ledger,
    load_settings,
    make_session,
    reconcile_ledger_with_wp,
    save_settings,
    write_manifest_template,
)


def _seo_status_message(site, sess) -> str:
    plugin = detect_seo_plugin(site, sess)
    if plugin != "none":
        return f"{plugin} (meta writable via REST)"
    installed = detect_seo_plugin_installed(site, sess)
    if installed != "none":
        return f"{installed} installed, but meta not REST-writable — using excerpt fallback"
    return "none (using excerpt)"

APP_DIR = Path(__file__).resolve().parent
app = Flask(__name__, template_folder=str(APP_DIR / "templates"), static_folder=str(APP_DIR / "static"))
app.secret_key = secrets.token_hex(16)
app.config["MAX_CONTENT_LENGTH"] = 5 * 1024 * 1024  # 5 MB form ceiling


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _parse_form_to_configs(form) -> tuple[SiteConfig, Path, ScheduleConfig, str, bool, list[str]]:
    """Extract typed configs from the submitted form. Returns (site, folder, schedule, status, overwrite, errors)."""
    errors: list[str] = []

    website = (form.get("website") or "").strip().rstrip("/")
    rest_path = (form.get("rest_path") or importer.DEFAULT_REST_PATH).strip()
    if not rest_path.startswith("/"):
        rest_path = "/" + rest_path
    username = (form.get("username") or "").strip()
    app_password = (form.get("app_password") or "").strip()
    folder_str = (form.get("folder") or "").strip()

    if not website:
        errors.append("Website URL is required.")
    if not website.startswith(("http://", "https://")):
        errors.append("Website URL must start with http:// or https://")
    if not username:
        errors.append("Username is required.")
    if not app_password:
        errors.append("Application Password is required.")
    if not folder_str:
        errors.append("Folder path is required.")

    folder = Path(folder_str).expanduser() if folder_str else Path()
    if folder_str and not folder.is_dir():
        errors.append(f"Folder does not exist or is not a directory: {folder}")

    try:
        start_date = dt.date.fromisoformat((form.get("start_date") or dt.date.today().isoformat()))
    except ValueError:
        errors.append("Start date must be YYYY-MM-DD.")
        start_date = dt.date.today()

    try:
        h, m = (form.get("time_of_day") or "09:00").split(":")
        time_of_day = dt.time(int(h), int(m))
    except Exception:
        errors.append("Time of day must be HH:MM.")
        time_of_day = dt.time(9, 0)

    try:
        timezone_offset = float(form.get("timezone_offset") or "10")
    except ValueError:
        errors.append("Timezone offset must be a number (hours, e.g. 10 for AEST).")
        timezone_offset = 10.0

    direction = (form.get("direction") or "backward").strip()
    if direction not in ("backward", "forward"):
        direction = "backward"

    try:
        days_between = int(form.get("days_between") or "1")
        if days_between < 1:
            days_between = 1
    except ValueError:
        days_between = 1

    status = (form.get("status") or "draft").strip()
    if status not in ("draft", "publish", "future"):
        status = "draft"

    overwrite = bool(form.get("overwrite"))

    site = SiteConfig(
        website=website,
        rest_path=rest_path,
        username=username,
        app_password=app_password,
    )
    schedule = ScheduleConfig(
        start_date=start_date,
        time_of_day=time_of_day,
        timezone_offset_hours=timezone_offset,
        direction=direction,
        days_between=days_between,
    )

    return site, folder, schedule, status, overwrite, errors


def _settings_to_form_defaults(folder: Path) -> dict:
    if not folder.exists():
        return {}
    settings = load_settings(folder)
    return settings


def _persist_safe_settings(folder: Path, form) -> None:
    if not folder.is_dir():
        return
    save_settings(
        folder,
        {
            "website": (form.get("website") or "").strip().rstrip("/"),
            "rest_path": (form.get("rest_path") or importer.DEFAULT_REST_PATH).strip(),
            "username": (form.get("username") or "").strip(),
            "start_date": form.get("start_date") or "",
            "time_of_day": form.get("time_of_day") or "09:00",
            "timezone_offset": form.get("timezone_offset") or "10",
            "direction": form.get("direction") or "backward",
            "days_between": form.get("days_between") or "1",
            "status": form.get("status") or "draft",
        },
    )


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #

@app.route("/")
def index():
    folder_str = request.args.get("folder", "").strip()
    defaults: dict = {}

    if folder_str:
        f = Path(folder_str).expanduser()
        if f.is_dir():
            defaults = _settings_to_form_defaults(f)
            defaults["folder"] = str(f)
    else:
        # Fall back to the form values stashed during the last /preview so
        # browser-back and the in-page Back link both repopulate the form.
        stashed = session.get("form") or {}
        if stashed:
            defaults.update({k: v for k, v in stashed.items() if v not in (None, "")})
            stashed_folder = (stashed.get("folder") or "").strip()
            if stashed_folder:
                f = Path(stashed_folder).expanduser()
                if f.is_dir():
                    file_settings = _settings_to_form_defaults(f)
                    for k, v in file_settings.items():
                        if v not in (None, ""):
                            defaults[k] = v
                    defaults["folder"] = str(f)

    return render_template(
        "form.html",
        defaults=defaults,
        today=dt.date.today().isoformat(),
    )


@app.route("/pick-folder", methods=["POST"])
def pick_folder():
    """Pop a native folder picker and return its absolute POSIX path.

    macOS-only (uses osascript). Falls back with an error JSON on other OSes
    or if the AppleScript runtime is missing.
    """
    script = (
        'POSIX path of (choose folder with prompt '
        '"Pick the folder with your blog .docx / .md files")'
    )
    try:
        proc = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=120,
        )
    except FileNotFoundError:
        return jsonify({
            "ok": False,
            "error": "osascript not available — folder picker requires macOS.",
        })
    except subprocess.TimeoutExpired:
        return jsonify({"ok": False, "error": "Picker timed out (120s)."})

    if proc.returncode != 0:
        msg = (proc.stderr or "").strip()
        if proc.returncode == 1 or "User canceled" in msg:
            return jsonify({"ok": False, "cancelled": True})
        return jsonify({"ok": False, "error": msg or "Picker failed"})

    path = proc.stdout.strip().rstrip("/")
    return jsonify({"ok": True, "path": path})


@app.route("/load-settings", methods=["POST"])
def load_settings_for_folder():
    folder_str = (request.form.get("folder") or "").strip()
    f = Path(folder_str).expanduser() if folder_str else None
    if not f or not f.is_dir():
        return jsonify({"ok": False, "error": "folder not found"})
    return jsonify({"ok": True, "settings": load_settings(f)})


@app.route("/preview", methods=["POST"])
def preview():
    site, folder, schedule, status, overwrite, errors = _parse_form_to_configs(request.form)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("index"))

    sess = make_session()
    ok, msg = check_credentials(site, sess)
    if not ok:
        flash(f"Credential check failed: {msg}", "error")
        return redirect(url_for("index"))

    seo_plugin = _seo_status_message(site, sess)
    ledger = load_ledger(folder)

    files = list_source_files(folder)
    if not files:
        flash(f"No .docx or .md files found in {folder}", "error")
        return redirect(url_for("index"))

    posts = build_parsed_posts(
        folder=folder,
        schedule=schedule,
        default_status=status,
        site_url=site.website,
        ledger=ledger,
    )

    cleared = reconcile_ledger_with_wp(posts, site, sess, ledger, folder)
    if cleared:
        flash(
            f"Cleared stale ledger entries (WP posts were deleted): {', '.join(cleared)}. "
            "These files will re-import as new.",
            "info",
        )

    _persist_safe_settings(folder, request.form)

    # Stash the form values (minus password) so /import can re-use them.
    session["form"] = {k: v for k, v in request.form.items() if k != "app_password"}
    session["folder"] = str(folder)

    return render_template(
        "results.html",
        mode="preview",
        site=site.website,
        folder=str(folder),
        seo_plugin=seo_plugin,
        auth_message=msg,
        posts=[asdict(p) for p in posts],
        overwrite=overwrite,
    )


@app.route("/import", methods=["POST"])
def import_now():
    site, folder, schedule, status, overwrite, errors = _parse_form_to_configs(request.form)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("index"))

    sess = make_session()
    ok, msg = check_credentials(site, sess)
    if not ok:
        flash(f"Credential check failed: {msg}", "error")
        return redirect(url_for("index"))

    files = list_source_files(folder)
    if not files:
        flash(f"No .docx or .md files found in {folder}", "error")
        return redirect(url_for("index"))

    _persist_safe_settings(folder, request.form)

    # Stash the configs (including password — server-side session only, never written to disk).
    session["import_job"] = {
        "site": asdict(site),
        "folder": str(folder),
        "schedule": {
            "start_date": schedule.start_date.isoformat(),
            "time_of_day": schedule.time_of_day.strftime("%H:%M"),
            "timezone_offset_hours": schedule.timezone_offset_hours,
            "direction": schedule.direction,
            "days_between": schedule.days_between,
        },
        "status": status,
        "overwrite": overwrite,
    }
    return render_template(
        "results.html",
        mode="import",
        site=site.website,
        folder=str(folder),
        seo_plugin=detect_seo_plugin(site, sess),
        auth_message=msg,
        posts=[],
        overwrite=overwrite,
    )


@app.route("/import/stream")
def import_stream():
    job = session.get("import_job")
    if not job:
        return Response("data: {\"event\": \"error\", \"message\": \"No import job in session\"}\n\n",
                        mimetype="text/event-stream")

    site = SiteConfig(**job["site"])
    sched = job["schedule"]
    schedule = ScheduleConfig(
        start_date=dt.date.fromisoformat(sched["start_date"]),
        time_of_day=dt.time(*[int(x) for x in sched["time_of_day"].split(":")]),
        timezone_offset_hours=sched["timezone_offset_hours"],
        direction=sched["direction"],
        days_between=sched["days_between"],
    )
    folder = Path(job["folder"])
    status = job["status"]
    overwrite = job["overwrite"]

    def event_stream():
        for ev in importer.run_import(folder, site, schedule, status, overwrite):
            yield f"data: {json.dumps(ev)}\n\n"

    return Response(stream_with_context(event_stream()), mimetype="text/event-stream")


@app.route("/download-manifest", methods=["POST"])
def download_manifest():
    site, folder, schedule, status, overwrite, errors = _parse_form_to_configs(request.form)
    if errors:
        for e in errors:
            flash(e, "error")
        return redirect(url_for("index"))
    posts = build_parsed_posts(
        folder=folder,
        schedule=schedule,
        default_status=status,
        site_url=site.website,
    )
    path = write_manifest_template(folder, posts)
    return send_file(path, as_attachment=True, download_name="manifest.csv")


@app.errorhandler(404)
def _404(_):
    return redirect(url_for("index"))


# --------------------------------------------------------------------------- #
# Entrypoint
# --------------------------------------------------------------------------- #

def _open_browser_later(url: str, delay: float = 0.7):
    def go():
        time.sleep(delay)
        try:
            webbrowser.open(url)
        except Exception:
            pass
    threading.Thread(target=go, daemon=True).start()


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=5050)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--debug", action="store_true")
    args = ap.parse_args()

    url = f"http://{args.host}:{args.port}"
    print(f"WordPress Blog Importer running at {url}", file=sys.stderr)
    print("Press Ctrl+C to stop.", file=sys.stderr)
    if not args.no_browser:
        _open_browser_later(url)
    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
