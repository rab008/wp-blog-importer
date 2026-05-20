"""Core import logic: SEO derivation, plugin detection, ledger, WordPress REST calls.

Web-framework-agnostic — used by app.py and reusable from a future CLI.
"""

from __future__ import annotations

import csv
import datetime as dt
import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Iterable

import requests
from requests.auth import HTTPBasicAuth

from parsers import SUPPORTED_EXTENSIONS, parse_file

DEFAULT_REST_PATH = "/wp-json/wp/v2"
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_0) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)
LEDGER_FILENAME = ".imported.json"
SETTINGS_FILENAME = ".wpimport.json"
MANIFEST_FILENAME = "manifest.csv"
CATEGORIES_FILENAME = "categories.json"

MANIFEST_COLUMNS = [
    "filename",
    "title",
    "slug",
    "meta_title",
    "meta_description",
    "category",
    "tags",
    "post_date",
    "status",
]


# --------------------------------------------------------------------------- #
# Data types
# --------------------------------------------------------------------------- #

@dataclass
class SiteConfig:
    website: str
    rest_path: str
    username: str
    app_password: str

    @property
    def api(self) -> str:
        return self.website.rstrip("/") + self.rest_path

    @property
    def admin_edit_url_template(self) -> str:
        return self.website.rstrip("/") + "/wp-admin/post.php?post={id}&action=edit"


@dataclass
class ScheduleConfig:
    start_date: dt.date
    time_of_day: dt.time
    timezone_offset_hours: float  # e.g. 10.0 for AEST
    direction: str  # "backward" or "forward"
    days_between: int


@dataclass
class ParsedPost:
    filename: str
    title: str
    body_html: str
    meta_title: str
    meta_description: str
    slug: str
    category: str | None
    tags: list[str]
    post_date_local: str  # ISO 8601, no tz
    post_date_gmt: str    # ISO 8601, no tz
    status: str
    override_post_id: int | None = None  # set when ledger says we'd overwrite


@dataclass
class ImportResult:
    filename: str
    status: str  # "OK", "SKIP", "FAIL"
    post_id: int | None = None
    edit_url: str | None = None
    message: str = ""


# --------------------------------------------------------------------------- #
# HTTP session
# --------------------------------------------------------------------------- #

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT, "Accept": "application/json"})
    return s


def auth_for(site: SiteConfig) -> HTTPBasicAuth:
    return HTTPBasicAuth(site.username, site.app_password)


def check_credentials(site: SiteConfig, session: requests.Session) -> tuple[bool, str]:
    """Return (ok, message). Hits /users/me."""
    try:
        r = session.get(f"{site.api}/users/me", auth=auth_for(site), timeout=15)
    except Exception as e:
        return False, f"Network error: {e}"
    if r.status_code == 200:
        data = r.json()
        return True, f"Authenticated as '{data.get('name', site.username)}' (id={data.get('id')})"
    if r.status_code in (401, 403):
        return False, f"Auth failed: HTTP {r.status_code}. Check username + Application Password."
    return False, f"Unexpected response: HTTP {r.status_code}"


def wp_post_exists(post_id: int, site: SiteConfig, session: requests.Session) -> bool:
    """True if the WP post is still retrievable. 404 → False (permanently deleted).

    Trash returns 200 (with status='trash') when authed with context=edit, so we
    treat trashed posts as 'exists' to avoid creating a duplicate while the user
    can still restore from trash. Network errors fail safe to True so a transient
    blip doesn't wipe the ledger.
    """
    try:
        r = session.get(
            f"{site.api}/posts/{post_id}",
            params={"context": "edit"},
            auth=auth_for(site),
            timeout=15,
        )
    except Exception:
        return True
    return r.status_code != 404


# --------------------------------------------------------------------------- #
# SEO derivation
# --------------------------------------------------------------------------- #

_KEEP_CHARS = re.compile(r"[^a-z0-9]+")


def slugify(text: str) -> str:
    s = text.lower()
    s = _KEEP_CHARS.sub("-", s)
    return s.strip("-")[:75] or "post"


def _truncate_on_word(text: str, limit: int) -> str:
    text = text.strip()
    if len(text) <= limit:
        return text
    cut = text[: limit + 1]
    if " " in cut:
        cut = cut.rsplit(" ", 1)[0]
    return cut.rstrip(" ,.;:-") + "…"


def derive_meta_title(title: str) -> str:
    return _truncate_on_word(title, 60)


def derive_meta_description(body_html: str) -> str:
    m = re.search(r"<p>(.*?)</p>", body_html, re.S)
    if not m:
        return ""
    raw = re.sub(r"<[^>]+>", "", m.group(1))
    raw = re.sub(r"\s+", " ", raw).strip()
    return _truncate_on_word(raw, 155)


# --------------------------------------------------------------------------- #
# SEO plugin detection
# --------------------------------------------------------------------------- #

YOAST_KEYS = ("_yoast_wpseo_title", "_yoast_wpseo_metadesc")
RANK_MATH_KEYS = ("rank_math_title", "rank_math_description")


def detect_seo_plugin(site: SiteConfig, session: requests.Session) -> str:
    """Returns 'yoast', 'rankmath', or 'none'.

    Only returns a plugin name when its meta keys are *actually writable* via
    the REST API (registered in the post type's meta schema). A plugin can be
    installed and active but still not expose its fields to REST writes — both
    Yoast and RankMath have this default. In that case we return 'none' so
    callers fall back to setting the standard `excerpt`.
    """
    try:
        r = session.get(
            f"{site.api}/types/post",
            params={"context": "edit"},
            auth=auth_for(site),
            timeout=15,
        )
        if r.status_code != 200:
            return "none"
        schema = r.json().get("schema", {}) or {}
        meta_props = (
            schema.get("properties", {})
            .get("meta", {})
            .get("properties", {})
            or {}
        )
        keys = set(meta_props.keys())
        if any(k in keys for k in YOAST_KEYS):
            return "yoast"
        if any(k in keys for k in RANK_MATH_KEYS):
            return "rankmath"
    except Exception:
        pass
    return "none"


def detect_seo_plugin_installed(site: SiteConfig, session: requests.Session) -> str:
    """Independent of REST writability: just whether the plugin is installed.

    Returns 'yoast', 'rankmath', or 'none' based on whether the plugin's REST
    namespace is reachable. Used to surface a helpful note to the user when
    the plugin is installed but its meta fields aren't writable via REST.
    """
    for plugin, ns in (("yoast", "yoast/v1"), ("rankmath", "rankmath/v1")):
        try:
            r = session.get(
                f"{site.website.rstrip('/')}/wp-json/{ns}",
                auth=auth_for(site),
                timeout=10,
            )
            if r.status_code == 200:
                return plugin
        except Exception:
            continue
    return "none"


def seo_meta_payload(plugin: str, meta_title: str, meta_description: str) -> dict:
    if plugin == "yoast":
        return {"_yoast_wpseo_title": meta_title, "_yoast_wpseo_metadesc": meta_description}
    if plugin == "rankmath":
        return {"rank_math_title": meta_title, "rank_math_description": meta_description}
    return {}


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #

def load_manifest(folder: Path) -> dict[str, dict]:
    path = folder / MANIFEST_FILENAME
    if not path.exists():
        return {}
    out: dict[str, dict] = {}
    with path.open(newline="", encoding="utf-8") as f:
        reader = csv.DictReader(f)
        for row in reader:
            fname = (row.get("filename") or "").strip()
            if not fname:
                continue
            out[fname] = {k: (v.strip() if isinstance(v, str) else v) for k, v in row.items()}
    return out


def write_manifest_template(folder: Path, posts: Iterable[ParsedPost]) -> Path:
    path = folder / MANIFEST_FILENAME
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=MANIFEST_COLUMNS)
        writer.writeheader()
        for p in posts:
            writer.writerow(
                {
                    "filename": p.filename,
                    "title": p.title,
                    "slug": p.slug,
                    "meta_title": p.meta_title,
                    "meta_description": p.meta_description,
                    "category": p.category or "",
                    "tags": ",".join(p.tags),
                    "post_date": p.post_date_local,
                    "status": p.status,
                }
            )
    return path


# --------------------------------------------------------------------------- #
# Ledger
# --------------------------------------------------------------------------- #

def load_ledger(folder: Path) -> dict:
    path = folder / LEDGER_FILENAME
    if not path.exists():
        return {"site": "", "imports": {}}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"site": "", "imports": {}}


def save_ledger(folder: Path, data: dict) -> None:
    (folder / LEDGER_FILENAME).write_text(
        json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8"
    )


def ledger_entry_for(ledger: dict, site_url: str, filename: str) -> dict | None:
    if (ledger.get("site") or "").rstrip("/") != site_url.rstrip("/"):
        return None
    return ledger.get("imports", {}).get(filename)


def reconcile_ledger_with_wp(
    posts: list["ParsedPost"],
    site: SiteConfig,
    session: requests.Session,
    ledger: dict,
    folder: Path,
) -> list[str]:
    """Drop ledger entries whose WP post no longer exists.

    For each post carrying an override_post_id (from a prior import), verify the
    post is still reachable on WP. If it's 404, clear override_post_id on the
    post and remove its filename from the ledger so the next import creates a
    fresh post. Returns the list of filenames that were cleared.
    """
    cleared: list[str] = []
    for post in posts:
        if not post.override_post_id:
            continue
        if not wp_post_exists(post.override_post_id, site, session):
            cleared.append(post.filename)
            ledger.get("imports", {}).pop(post.filename, None)
            post.override_post_id = None
    if cleared:
        save_ledger(folder, ledger)
    return cleared


# --------------------------------------------------------------------------- #
# Per-folder settings persistence (no password!)
# --------------------------------------------------------------------------- #

def load_settings(folder: Path) -> dict:
    path = folder / SETTINGS_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def save_settings(folder: Path, settings: dict) -> None:
    safe = {k: v for k, v in settings.items() if k != "app_password"}
    (folder / SETTINGS_FILENAME).write_text(
        json.dumps(safe, ensure_ascii=False, indent=2), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Date scheduling
# --------------------------------------------------------------------------- #

def date_for_index(schedule: ScheduleConfig, index: int) -> tuple[str, str]:
    sign = -1 if schedule.direction == "backward" else 1
    offset_days = sign * index * schedule.days_between
    target_date = schedule.start_date + dt.timedelta(days=offset_days)
    local_dt = dt.datetime.combine(target_date, schedule.time_of_day)
    gmt_dt = local_dt - dt.timedelta(hours=schedule.timezone_offset_hours)
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S"), gmt_dt.strftime("%Y-%m-%dT%H:%M:%S")


def parse_manifest_date(value: str, schedule: ScheduleConfig) -> tuple[str, str] | None:
    if not value:
        return None
    v = value.strip()
    if not v:
        return None
    # Full ISO: 2026-05-14T09:00:00
    if "T" in v:
        try:
            local_dt = dt.datetime.fromisoformat(v.split("+")[0].split("Z")[0])
        except ValueError:
            return None
    else:
        try:
            d = dt.date.fromisoformat(v)
        except ValueError:
            return None
        local_dt = dt.datetime.combine(d, schedule.time_of_day)
    gmt_dt = local_dt - dt.timedelta(hours=schedule.timezone_offset_hours)
    return local_dt.strftime("%Y-%m-%dT%H:%M:%S"), gmt_dt.strftime("%Y-%m-%dT%H:%M:%S")


# --------------------------------------------------------------------------- #
# Build ParsedPost list (preview / dry-run)
# --------------------------------------------------------------------------- #

def list_source_files(folder: Path) -> list[Path]:
    files = [
        p for p in folder.iterdir()
        if p.is_file() and p.suffix.lower() in SUPPORTED_EXTENSIONS
    ]
    files.sort(key=lambda p: p.name.lower())
    return files


def build_parsed_posts(
    folder: Path,
    schedule: ScheduleConfig,
    default_status: str,
    site_url: str,
    ledger: dict | None = None,
) -> list[ParsedPost]:
    manifest = load_manifest(folder)
    rules = load_category_rules(folder)
    files = list_source_files(folder)
    posts: list[ParsedPost] = []

    for i, path in enumerate(files):
        title, body_html = parse_file(path)
        row = manifest.get(path.name, {}) or {}

        if row.get("title"):
            title = row["title"]

        meta_title = row.get("meta_title") or derive_meta_title(title)
        meta_description = row.get("meta_description") or derive_meta_description(body_html)
        slug = row.get("slug") or slugify(title)

        category = row.get("category") or None
        if not category:
            category = detect_category(title, body_html, rules)
        tags = [t.strip() for t in (row.get("tags") or "").split(",") if t.strip()]

        manifest_date = parse_manifest_date(row.get("post_date", ""), schedule)
        local_iso, gmt_iso = manifest_date if manifest_date else date_for_index(schedule, i)

        status = (row.get("status") or "").strip() or default_status

        override_id = None
        if ledger is not None:
            entry = ledger_entry_for(ledger, site_url, path.name)
            if entry:
                override_id = entry.get("post_id")

        posts.append(
            ParsedPost(
                filename=path.name,
                title=title,
                body_html=body_html,
                meta_title=meta_title,
                meta_description=meta_description,
                slug=slug,
                category=category,
                tags=tags,
                post_date_local=local_iso,
                post_date_gmt=gmt_iso,
                status=status,
                override_post_id=override_id,
            )
        )

    return posts


# --------------------------------------------------------------------------- #
# Category resolution
# --------------------------------------------------------------------------- #

def load_category_rules(folder: Path) -> dict:
    """Load per-folder keyword-based category rules.

    Schema:
      {
        "default": "Uncategorized",
        "rules": {
          "Cooking": ["recipe", "ingredient", "bake"],
          "Travel":  ["destination", "flight", "hotel"]
        }
      }
    """
    path = folder / CATEGORIES_FILENAME
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def detect_category(title: str, body_html: str, rules: dict) -> str | None:
    """Score post text against keyword rules; return the highest-scoring category."""
    if not rules:
        return None
    rule_map = rules.get("rules") or {}
    default = (rules.get("default") or "").strip() or None
    if not rule_map:
        return default
    plain = re.sub(r"<[^>]+>", " ", body_html).lower()
    haystack = f"{title.lower()} {plain}"
    scores: dict[str, int] = {}
    for category, keywords in rule_map.items():
        score = 0
        for kw in keywords or []:
            k = kw.lower().strip()
            if not k:
                continue
            score += len(re.findall(r"\b" + re.escape(k) + r"\b", haystack))
        if score > 0:
            scores[category] = score
    if not scores:
        return default
    return max(scores.items(), key=lambda kv: kv[1])[0]


def fetch_categories(site: SiteConfig, session: requests.Session) -> dict[str, int]:
    r = session.get(
        f"{site.api}/categories",
        params={"per_page": 100},
        auth=auth_for(site),
        timeout=20,
    )
    r.raise_for_status()
    return {c["name"]: c["id"] for c in r.json()}


def create_category(name: str, site: SiteConfig, session: requests.Session) -> int:
    r = session.post(
        f"{site.api}/categories",
        json={"name": name},
        auth=auth_for(site),
        timeout=20,
    )
    if r.status_code in (200, 201):
        return r.json()["id"]
    if r.status_code == 400 and "term_exists" in r.text:
        existing = r.json().get("data", {}).get("term_id")
        if existing:
            return int(existing)
    r.raise_for_status()
    return r.json()["id"]


def resolve_categories(
    needed_names: list[str],
    site: SiteConfig,
    session: requests.Session,
) -> dict[str, int]:
    existing = fetch_categories(site, session)
    for name in needed_names:
        if name and name not in existing:
            existing[name] = create_category(name, site, session)
    return existing


# --------------------------------------------------------------------------- #
# Import a single post
# --------------------------------------------------------------------------- #

def import_one_post(
    post: ParsedPost,
    site: SiteConfig,
    session: requests.Session,
    seo_plugin: str,
    category_id: int | None,
    overwrite: bool,
) -> ImportResult:
    payload = {
        "title": post.title,
        "content": post.body_html,
        "excerpt": post.meta_description,
        "slug": post.slug,
        "status": post.status,
        "date": post.post_date_local,
        "date_gmt": post.post_date_gmt,
    }
    if category_id:
        payload["categories"] = [category_id]
    meta = seo_meta_payload(seo_plugin, post.meta_title, post.meta_description)
    if meta:
        payload["meta"] = meta

    edit_template = site.admin_edit_url_template

    if overwrite and post.override_post_id:
        url = f"{site.api}/posts/{post.override_post_id}"
        r = session.post(url, json=payload, auth=auth_for(site), timeout=60)
        if r.status_code in (200, 201):
            data = r.json()
            return ImportResult(
                filename=post.filename,
                status="OK",
                post_id=data["id"],
                edit_url=edit_template.format(id=data["id"]),
                message="updated",
            )
        return ImportResult(
            filename=post.filename,
            status="FAIL",
            message=f"HTTP {r.status_code}: {r.text[:300]}",
        )

    r = session.post(f"{site.api}/posts", json=payload, auth=auth_for(site), timeout=60)
    if r.status_code in (200, 201):
        data = r.json()
        return ImportResult(
            filename=post.filename,
            status="OK",
            post_id=data["id"],
            edit_url=edit_template.format(id=data["id"]),
            message="created",
        )
    return ImportResult(
        filename=post.filename,
        status="FAIL",
        message=f"HTTP {r.status_code}: {r.text[:300]}",
    )


# --------------------------------------------------------------------------- #
# Top-level import driver (generator → yields per-post results)
# --------------------------------------------------------------------------- #

def run_import(
    folder: Path,
    site: SiteConfig,
    schedule: ScheduleConfig,
    default_status: str,
    overwrite: bool,
):
    session = make_session()

    ledger = load_ledger(folder)
    if not ledger.get("site"):
        ledger["site"] = site.website
    if not ledger.get("imports"):
        ledger["imports"] = {}

    seo_plugin = detect_seo_plugin(site, session)
    if seo_plugin == "none":
        installed = detect_seo_plugin_installed(site, session)
        if installed != "none":
            yield {
                "event": "status",
                "message": (
                    f"{installed} is installed but its meta fields aren't writable via REST. "
                    "Falling back to standard 'excerpt' for meta description. "
                    "(To enable, register the meta fields with show_in_rest=true in a small mu-plugin.)"
                ),
            }
        else:
            yield {"event": "status", "message": "No SEO plugin detected — using standard 'excerpt' for meta description."}
    else:
        yield {"event": "status", "message": f"SEO plugin detected: {seo_plugin} (meta writable via REST)"}

    posts = build_parsed_posts(
        folder=folder,
        schedule=schedule,
        default_status=default_status,
        site_url=site.website,
        ledger=ledger,
    )

    cleared = reconcile_ledger_with_wp(posts, site, session, ledger, folder)
    for fn in cleared:
        yield {
            "event": "status",
            "message": f"Ledger entry for '{fn}' was stale (WP post deleted) — will re-import as new.",
        }

    # Pre-resolve categories.
    needed = sorted({p.category for p in posts if p.category})
    cat_map: dict[str, int] = {}
    if needed:
        yield {"event": "status", "message": f"Resolving {len(needed)} categories…"}
        try:
            cat_map = resolve_categories(needed, site, session)
        except Exception as e:
            yield {"event": "status", "message": f"Category resolution failed: {e}"}

    for post in posts:
        if post.override_post_id and not overwrite:
            yield {
                "event": "result",
                "result": asdict(
                    ImportResult(
                        filename=post.filename,
                        status="SKIP",
                        post_id=post.override_post_id,
                        edit_url=site.admin_edit_url_template.format(id=post.override_post_id),
                        message="already imported — tick 'Re-import all' to overwrite",
                    )
                ),
            }
            continue

        category_id = cat_map.get(post.category) if post.category else None
        result = import_one_post(
            post=post,
            site=site,
            session=session,
            seo_plugin=seo_plugin,
            category_id=category_id,
            overwrite=overwrite,
        )
        if result.status == "OK" and result.post_id:
            ledger["imports"][post.filename] = {
                "post_id": result.post_id,
                "imported_at": dt.datetime.now().isoformat(timespec="seconds"),
                "url": f"{site.website.rstrip('/')}/?p={result.post_id}",
            }
            save_ledger(folder, ledger)

        yield {"event": "result", "result": asdict(result)}
        time.sleep(0.4)

    yield {"event": "done"}
