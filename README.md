# WordPress Blog Importer

A small local web app that imports a folder of `.docx` or `.md` blog posts into any WordPress site as drafts (or scheduled / published posts) via the REST API.

## One-time setup

```bash
pip3 install -r ~/wp-blog-importer/requirements.txt
```

You also need an **Application Password** on your WordPress user. Generate it in WP Admin → Users → Profile → Application Passwords.

## Run

```bash
python3 ~/wp-blog-importer/app.py
```

A browser tab will open at `http://127.0.0.1:5050`. Fill in the form:

| Field | Notes |
|---|---|
| Website URL | e.g. `https://www.example.com` |
| REST API path | Default `/wp-json/wp/v2`. Leave as-is for most sites. |
| Username | Your WordPress login (not your email unless email logins are enabled). |
| Application Password | 24-character app password. Never stored on disk. |
| Folder location | Click **📁 Browse folder…** to pick the folder via a Finder dialog. The folder should contain your `.docx` / `.md` files (and optional `manifest.csv`). macOS only; Linux / Windows builds will need a different picker (use DevTools to set `#folder.value` directly as a workaround). |
| Start date / Time / Timezone / Direction / Days between | Controls the post date for each file. |
| Status | `draft` (default), `publish`, or `future` (scheduled). |
| Re-import all | Tick this to PATCH posts that were previously imported (looked up in `.imported.json`). |

Three buttons:

- **Preview (dry-run)** — parses every file, derives SEO fields, shows a table of what *would* be imported. No write calls.
- **Import now** — runs the real import; streams progress one row at a time, each linking to its WP-admin edit page.
- **Generate manifest.csv template** — writes a CSV next to your files (one row per post) so you can edit titles / slugs / dates / categories in Excel and re-preview.

## File conventions

Drop your content files in a single folder:

```
my-batch/
├── 01 Post About Coffee.docx
├── 02 Post About Tea.md
├── manifest.csv          (optional)
└── .imported.json        (auto-managed, do not delete)
```

Files are imported in **alphabetical order** of their filename. To control order, prefix with numbers (`01 …`, `02 …`).

### manifest.csv (optional)

**Starter template:** a ready-to-use sample lives at `~/wp-blog-importer/manifest.example.csv`. Copy it into your content folder, rename to `manifest.csv`, and edit. It contains 5 example rows showing the most common override patterns (full override, SEO-only, classification-only, scheduled future post, slug-only).

Columns (all optional except `filename`):

```
filename,title,slug,meta_title,meta_description,category,tags,post_date,status
```

Empty cells fall back to auto-derived defaults:

- `title` ← H1 from the document
- `slug` ← kebab-case of title
- `meta_title` ← title truncated to 60 chars on a word boundary
- `meta_description` ← first paragraph stripped & truncated to 155 chars
- `post_date` ← scheduled date based on form settings (YYYY-MM-DD or full ISO)
- `category` ← unset (no category) — write a category *name*; the app creates it if missing
- `tags` ← comma-separated
- `status` ← form default

The fastest workflow: click **Generate manifest.csv template** on the form, edit in your spreadsheet app, save back, then click **Preview**.

### Using manifest.csv (step-by-step)

Three starting paths — pick whichever matches your situation.

#### Path A — Start from the example template

Use this when you don't have content yet and want to see the format first.

```bash
cp ~/wp-blog-importer/manifest.example.csv /your/content-folder/manifest.csv
# edit manifest.csv in Excel / Numbers / Google Sheets
# drop your .docx / .md files into the same folder
```

Then run the app → Browse → pick the folder → Preview → Import.

#### Path B — Generate from your real files (recommended)

The most common workflow when you have a batch of content ready:

1. Drop your `.docx` / `.md` files into one folder (no subfolders).
2. Launch the app: `python3 ~/wp-blog-importer/app.py`
3. Fill the form — website URL, REST path, username, application password.
4. Click 📁 **Browse folder…** and pick the folder from step 1.
5. Click **Generate manifest.csv template**. The app:
   - Writes `manifest.csv` *into your folder* with one row per file, every field pre-filled with auto-derived defaults.
   - Also downloads a copy to your browser's Downloads folder (handy as a backup).
6. Open `manifest.csv` inside your content folder (Excel / Numbers / Sheets). Override only the fields you care about per post:
   - `title` — overrides H1 from the doc
   - `slug` — your SEO-friendly URL
   - `meta_title` — ≤ 60 chars
   - `meta_description` — ≤ 155 chars
   - `category` — name; the app creates it on the WP site if missing
   - `tags` — comma-separated
   - `post_date` — `YYYY-MM-DD` or full ISO (`2026-06-15T10:00:00`)
   - `status` — `draft`, `publish`, or `future`
   - **Leave any cell blank to fall back to the auto-derived default.**
7. Save the CSV, then back in the app click **Preview**. The table reflects your manifest overrides.
8. Click **Import now** — posts go up to WordPress as drafts (or whatever status you set).

#### Path C — Skip the manifest entirely

If auto-derived defaults are good enough (title from H1, slug = kebab-case, meta description from first paragraph), just drop files → Browse → Preview → Import. No manifest needed.

#### Re-import / overwriting

Each successful import is logged in `.imported.json` inside the folder. Re-running against the same folder *skips* already-imported files. If you edit the manifest and want those edits to actually update the WordPress posts, tick **Re-import all (overwrite)** on the form before clicking Import — the app will then PATCH the existing post id instead of creating a new draft.

### .imported.json (ledger)

Each successful import writes one entry to this file in the folder. Re-runs against the same folder *skip* already-imported files unless you tick "Re-import all".

## SEO plugin support

The app probes the site's REST API for known plugin meta fields:

- **Yoast SEO** → sets `_yoast_wpseo_title` and `_yoast_wpseo_metadesc`
- **Rank Math** → sets `rank_math_title` and `rank_math_description`
- **Neither / locked-down** → falls back to setting the standard WP `excerpt`

The detected plugin name appears in the preview header.

## Supported document features

`.docx`:
- Headings (Heading1 → post title, Heading2/3 → h2/h3)
- Paragraphs with bold / italic / hyperlinks
- Bulleted lists
- Tables (rendered as HTML `<table>`)
- FAQ pattern `<p><strong>Q: …?</strong> A: …</p>` is automatically promoted to `<h3>` + `<p>` with prefixes stripped

`.md` (CommonMark + GFM-ish subset):
- ATX headings (`#`, `##`, `###`)
- `**bold**`, `*italic*` / `_italic_`, `` `code` ``, `[text](url)`
- Bulleted (`-`, `*`, `+`) and ordered (`1.`) lists
- GFM tables
- Same FAQ promotion when a paragraph follows the `**Q: …?** A: …` pattern

## Files saved per folder

| File | Purpose |
|---|---|
| `.wpimport.json` | Caches form values (NOT the password) so re-opens against this folder pre-fill the form |
| `.imported.json` | Ledger — which file maps to which post id (skip logic) |
| `manifest.csv` | (Optional) per-post override sheet you wrote or generated |

## Troubleshooting

- **First Browse click prompts "Python wants to control Finder"** — macOS TCC permission. Click *OK* once and the picker will work for all future runs. Not a security issue: the server only runs on `127.0.0.1` and only you can hit it.
- **Browse folder… does nothing on Linux / Windows** — the picker uses macOS `osascript`. As a workaround, open browser DevTools (F12 → Console) and run `document.getElementById('folder').value = '/your/path'` to set the path manually.
- **403 Forbidden on first request** — the site's WAF is blocking the Python client. The app already sends a browser User-Agent; if it still fails, check Wordfence or Cloudflare rules.
- **401 Unauthorized** — the Application Password is wrong or the user lacks the `application_passwords` capability. Generate a fresh one in WP Admin.
- **"No .docx or .md files found"** — the folder path is correct but contains no supported files; check capitalization or hidden file managers.
- **Yoast / RankMath fields don't appear in WP admin after import** — the plugin's REST meta access might be locked; the app falls back to `excerpt`. You can paste the meta description manually, or extend the importer with a plugin-specific REST endpoint.

## Stopping the server

`Ctrl+C` in the terminal where it's running. The server is local-only (`127.0.0.1`) and not exposed to your network.
