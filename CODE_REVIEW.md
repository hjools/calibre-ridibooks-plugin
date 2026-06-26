# Code review — calibre-ridibooks-plugin

> **Status (2026-06-26, v1.0.8): network resilience + fallback matching.**
> Ridibooks' WAF intermittently resets the TLS connection (WinError 10054)
> when several requests arrive in quick succession; a single dropped connection
> surfaced to the user as "Found 0 results". `open_url` now retries with a short
> backoff (3 attempts) before giving up, so transient resets no longer fail a
> lookup. Two matching fixes shipped alongside: (1) the title-only fallback
> (used when an author-narrowed search returns nothing — calibre mangles short
> CJK author names, e.g. '구보 유키야' -> '유키야') now still *ranks* the broader
> result set by the original author, so a same-titled book by the wrong author
> no longer wins; (2) `_apply_series` no longer fabricates a "0권" suffix for
> standalone books that Ridibooks flags as a one-entry series with volume 0.
> The in-tree test suite was also brought in line with the current behaviour
> (volume titles normalise to "<series> N권"; Ridibooks now serves a revised
> edition of one test book) and all six cases pass end-to-end on calibre 9.9.
>
> **Status (2026-06-24): all findings below have been addressed in v1.0.1.**
>
> **Fixed:** the three live breakers (ISO publish-date parsing, 0–1→0–5 rating
> scale, null `keywords`); missing-meta `IndexError`; Python-3 `unicode()` in the
> config/utility code; Qt5/Qt4 imports migrated to `qt.core` with a PyQt5
> fallback; vendored `requests`/`libs/` removed in favour of calibre's
> `self.browser` (with timeouts); dead empty-credential login removed;
> non-functional `getEditions`/`getAllAuthors` config options removed;
> duplicated/fragile JSON-LD unescaping consolidated into one helper; invalid
> regex escape sequences; `mi.language` → `mi.languages=['kor']`; a minimal
> match-score floor; `minimum_calibre_version` corrected to `(5, 0, 0)`.
>
> **Found & fixed during live testing on calibre 9.9:** Ridibooks' WAF now
> returns HTTP 403 to calibre's mechanize browser regardless of User-Agent, so
> all requests were switched to stdlib `urllib` (search, book page, cover);
> `mi.isbn` was extracted but never actually assigned to the `Metadata` (so ISBN
> always came back empty) — now set; title+author searches could return nothing
> because calibre's author tokens are ANDed and may be partial, so a title-only
> retry was added; and tags now come from the search result's keyword chips
> (`tags_info`) plus the cleaned category and JSON-LD `genre` — restoring the
> full keyword list the old plugin showed (the old `info_category_wrap` markup
> and JSON-LD `keywords` are both gone); and series handling
> was reworked: the series name comes from the embedded `bookDetail` object,
> the volume index is taken from the searched title (Ridibooks' search always
> returns volume 1 of a grouped series, so the page can't tell which volume the
> user has), and the book title is normalised to `<series> <N>권`. Each volume's
> own book id (from the page's series list) is used as the `ridibooks`
> identifier and cover URL, so per-volume cover art is fetched correctly and a
> re-download self-corrects the per-volume ISBN/pubdate (the first title-search
> pass still reports volume 1's ISBN, since it lands on that page). End-to-end
> verified: identify by id and by title/author, plus cover download, all return
> correct data on calibre 9.9.
>
> **Intentionally deferred:** Qt6 *enum scoping* (e.g. `Qt.Checked`) was left on
> the short-name form — the plugin now loads and initialises cleanly on calibre
> 9.9 (Qt6), confirming the import path, though the config dialog's widgets
> weren't exercised in a live GUI; and some dormant helper classes/dialogs in
> `common_utils.py` (copied from the upstream Goodreads plugin) that this plugin
> never instantiates were modernised only where trivial.
>
> The original findings are preserved below as the record of what was changed.

---

Reviewed 2026-06-24 against the live ridibooks.com site. This is a genuine
calibre `Source` metadata plugin, forked from kiwidude's Goodreads plugin (per
the README) and adapted for Ridibooks. `__init__.py` is © Helen Lee; `config.py`
and `worker.py` still carry the upstream © Jin, Heonkyu — useful to know because
most of the dead code below is inherited from that fork, not hand-written here.

The architecture is sound: `identify()` hits the search API → picks the best
match → spawns a `Worker` thread that parses the book page into a `Metadata`.
The problems are in the parsing details, and three of them stop it working today.

---

## 🔴 Currently broken (verified against the live site)

These were confirmed by fetching the real search API and book page on
2026-06-24. The search API (`search-api.ridibooks.com`) and the book page's
`ld+json` / `og:` / `books:` meta tags are all still present and parseable — but:

### 1. Publish-date parser throws on the current date format → no results at all
`worker.py:73-77` `_format_date` assumes `datePublished` is `YYYYMMDD` and
slices it positionally:
```python
year = int(date_text[0:4]); month = int(date_text[4:6]); day = int(date_text[6:])
```
The site now returns **ISO format** `datePublished: "2014-11-19"`. So
`date_text[4:6]` → `"-1"` and `date_text[6:]` → `"1-19"`, and `int("1-19")`
raises `ValueError`. That exception propagates up through `load_details`, gets
swallowed by the bare `except` in `Worker.run` (`worker.py:59`), and **no
Metadata is ever put on the result queue** — i.e. every lookup silently returns
nothing.

**Fix:** parse robustly, e.g.
```python
from calibre.utils.date import parse_only_date
mi.pubdate = parse_only_date(book_info['datePublished'], assume_utc=True)
```

### 2. Ratings come out ~5× too low
`worker.py:145` does `mi.rating = float(books:rating:normalized_value)`. That meta
value is normalized to **0–1** (the live page returns `0.9` for a 4.5★ book),
but calibre's `mi.rating` is a **0–5** scale. So a 4.5-star book is stored as
`0.9` (≈ half a star). The `_normalize_score` helper (`worker.py:79`) divides by
5 — the wrong direction — and is never called anyway.

**Fix:** `mi.rating = max(0.0, min(5.0, float(value) * 5.0))` → `0.9` becomes `4.5`.

### 3. `parse_tags` crashes when `keywords` is null
`worker.py:184` guards with `if 'keywords' in book_info:`, but the live JSON-LD
contains `"keywords": null` (key present, value `None`). The next line
`keywords[1:len(keywords)-1]` then does `len(None)` → `TypeError`, which again
kills the whole detail parse for that book.

**Fix:** `kw = book_info.get('keywords'); if kw:` (truthy check, not `in`).

---

## Correctness / robustness

- **`_get_book_page` does a dead login and bypasses calibre's network stack**
  (`worker.py:82-103`). It POSTs to the login endpoint with **empty**
  `user_id`/`password` (leftover from the old scraper) on every book, then GETs
  the page with a fresh vendored-`requests` session. Public book pages need no
  login, and using `requests` skips calibre's proxy/timeout config. Use the
  passed-in `self.browser` (already cloned at `worker.py:38`) and drop the login.
- **`root` can be referenced unbound** (`worker.py:96-103`): if the request
  throws, the `except` only logs, then `return root` hits an undefined name.
  Re-raise or `return None` and handle.
- **`identify` has no timeout** (`__init__.py:121`): `requests.get(query).text`
  ignores both the `timeout` parameter and `br` — a hung connection hangs the
  GUI worker. Use `self.browser.open_novisit(query, timeout=timeout)`.
- **`_find_meta(...)[0]` IndexErrors on missing tags** (`worker.py:71`): a book
  without `books:isbn` will crash. Return `None` when the property is absent.
- **No real match threshold** (`__init__.py:205-209`): `_parse_search_results`
  always returns the highest-similarity result, even if similarity is ~0. The
  `if matched_book is None` "rejection" branch is dead — `matched_book` is always
  a dict. Add a minimum-score cutoff so a bad query doesn't return a wrong book.
- **Language is set wrong** (`worker.py:164`): `mi.language = 'Korean'` should be
  the ISO code (`mi.languages = ['kor']`). `touched_fields` even declares
  `languages` (plural). The `lang_map` built in `__init__` (`worker.py:40-54`) is
  never used.
- **Invalid regex escapes** (`worker.py:156,158`): `u'(.*)\s*(\d+)권'` should be a
  raw string `r'...'` (Python 3 warns on `\s`/`\d` in normal string literals).

## Python 3 / modern calibre compatibility

- **`config.py` will crash on save under Py3.** It uses `unicode(...)`
  (`config.py:163-164,175,181,276,307`), which doesn't exist in Python 3. Current
  calibre is Py3, and `get_data()` runs on **every config commit**, so saving the
  plugin settings raises `NameError`. Replace `unicode` → `str`.
- **Qt imports are PyQt5/PyQt4** (`config.py:13-21`). Calibre 6+ moved to Qt6 /
  PyQt6. Use calibre's version-agnostic shim: `from qt.core import (...)`.
  Otherwise the config dialog may fail to load on modern calibre.
- **`minimum_calibre_version = (0, 8, 0)` is misleading** (`__init__.py:34`): the
  code uses `urllib.parse`/`queue` (Py3-only), so it really needs calibre 5+.

## Dead code & cruft (mostly inherited from the Goodreads fork)

- **Vendored `libs/` (requests, urllib3, idna, certifi) is unnecessary bloat.**
  calibre ships `self.browser`; switching to it lets you delete the entire
  `libs/` tree and the matching lines in `build.sh`.
- **Two config options do nothing.** `KEY_GET_EDITIONS` and `KEY_GET_ALL_AUTHORS`
  render as checkboxes (`config.py:237-258`) but are never read anywhere in
  `__init__.py`/`worker.py`. Only `KEY_GENRE_MAPPINGS` is actually used. Either
  wire them up or remove them. Their tooltips also still say "Goodreads".
- **Unused locals / commented-out code**: `_normalize_score` (`worker.py:79`);
  `c = cfg.plugin_prefs[...]` in `_parse_search_results` (`__init__.py:216`);
  several commented `# response = br.open_novisit(...)` lines.
- **`parse_tags` re-parses the ld+json** that `load_details` already parsed
  (`worker.py:177-183` duplicates `worker.py:113-120`), including the same
  fragile manual `&quot;`/`&lt;` unescaping. Parse once and pass `book_info` in.
- **Committed artifacts**: `bin/*.zip` and `.DS_Store` files are in the repo. Add
  a `.gitignore` (`*.zip` under `bin/` if it's a build output, `**/.DS_Store`).

## The fragile unescaping

`worker.py:116-119` repairs the embedded JSON-LD by hand
(`&lt;`→`<`, `\&quot;`→`\\"`, `&quot;`→`\\"`) before `json.loads`. This is
order-sensitive and breaks if Ridibooks changes its escaping. Prefer extracting
the `<script>` text and running it through `html.unescape` once, or pull the
fields you need from the parsed JSON-LD rather than string-surgery.

---

## Suggested priority

1. Fix the three breakers (date, rating, null keywords) — ~5 lines total, makes
   the plugin work again.
2. Swap vendored `requests` for `self.browser` (with timeouts) and delete
   `libs/`.
3. `unicode`→`str` and the `qt.core` import migration for current-calibre config.
4. Add a match threshold + ISO-code language; clean up dead config/options.

I can apply 1–3 as a patch if you want — they're small and well-contained.
