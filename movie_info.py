"""Rotten Tomatoes score + short plot description for Rossmoor movies.

Results are cached in movie_info.json (committed by the sync workflow),
keyed by "title|year". The cache can be hand-edited: any entry with
"locked": true is never overwritten, so a corrected score or a better
description sticks.

Lookups are best-effort. If Rotten Tomatoes or Wikipedia are down or
change their markup, the movie still goes on the calendar, just without
a score or description, and the miss is retried on the next run.
"""

import html as html_lib
import json
import logging
import os
import re
import unicodedata
import urllib.parse
import urllib.request

log = logging.getLogger("movie_info")

CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                          "movie_info.json")
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0 Safari/537.36")


def _key(title, year):
    return "{}|{}".format(_norm(title), (year or "").strip())


def _norm(s):
    s = unicodedata.normalize("NFKD", s or "").encode("ascii", "ignore")
    s = s.decode().lower().replace("&", "and")
    s = re.sub(r"[^a-z0-9]+", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _get(url, timeout=15):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def _title_variants(title):
    """Rossmoor writes "Yacht Rock-A Documentary" / "Famous Last
    Words-Gloria Steinem"; RT would say "Yacht Rock: A DOCKumentary".
    Try the full title and the part before the dash."""
    out = [title]
    for sep in ("-", ":", " ("):
        if sep in title:
            out.append(title.split(sep)[0].strip())
    return [t for i, t in enumerate(out) if t and t not in out[:i]]


def lookup_rt(title, year):
    """Return (score_str, url) from RT search, or (None, None)."""
    try:
        year_i = int(year)
    except (TypeError, ValueError):
        year_i = None
    for variant in _title_variants(title):
        try:
            page = _get("https://www.rottentomatoes.com/search?search="
                        + urllib.parse.quote(variant))
        except Exception as e:
            log.info("    RT search failed for %r: %s", variant, e)
            return None, None
        rows = re.findall(
            r"<search-page-media-row(.*?)</search-page-media-row>",
            page, re.S)
        best = None
        for row in rows:
            score = re.search(r'tomatometer-score="(\d+)"', row)
            ry = re.search(r'release-year="(\d{4})"', row)
            name = re.search(r'data-qa="info-name"[^>]*>\s*([^<]+?)\s*</a>',
                             row)
            href = re.search(r'href="(https://www\.rottentomatoes\.com/m/'
                             r'[^"]+)"', row)
            if not (score and name and href):
                continue
            name_n = _norm(html_lib.unescape(name.group(1)))
            want = _norm(variant)
            title_ok = (name_n == want or name_n.startswith(want + " ")
                        or want.startswith(name_n + " "))
            year_ok = (year_i is None or (ry and abs(int(ry.group(1))
                                                     - year_i) <= 1))
            if title_ok and year_ok:
                exact = name_n == want and ry and year_i == int(ry.group(1))
                cand = (score.group(1) + "%", href.group(1))
                if exact:
                    return cand
                best = best or cand
        if best:
            return best
    return None, None


def lookup_description(title, year):
    """1-3 sentence summary from Wikipedia, or ""."""
    terms = ["{} ({} film)".format(title, year), "{} (film)".format(title),
             title]
    for term in terms:
        url = ("https://en.wikipedia.org/api/rest_v1/page/summary/"
               + urllib.parse.quote(term.replace(" ", "_")))
        try:
            data = json.loads(_get(url, timeout=10))
        except Exception:
            continue
        if data.get("type") == "disambiguation":
            continue
        extract = data.get("extract", "")
        # Guard against matching a person/place article for a short title
        if len(extract) > 40 and re.search(
                r"\b(film|movie|documentary|series|episode)\b",
                extract[:300], re.I):
            sentences = re.split(r"(?<=[.!?])\s+", extract)
            desc = " ".join(sentences[:3])
            return desc[:497] + "..." if len(desc) > 500 else desc
    return ""


def load_cache():
    try:
        with open(CACHE_PATH) as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_cache(cache):
    tmp = CACHE_PATH + ".tmp"
    with open(tmp, "w") as f:
        json.dump(cache, f, indent=1, ensure_ascii=False, sort_keys=True)
        f.write("\n")
    os.replace(tmp, CACHE_PATH)


def enrich(movies):
    """Add rt_score / rt_url / description to each movie dict in place."""
    cache = load_cache()
    changed = False
    seen = {}
    for m in movies:
        k = _key(m.get("title", ""), m.get("movie_year", ""))
        if k not in seen:
            entry = cache.get(k, {})
            missing_score = not entry.get("rt_score")
            missing_desc = not entry.get("description")
            if not entry.get("locked") and (missing_score or missing_desc):
                title, year = m.get("title", ""), m.get("movie_year", "")
                if missing_score:
                    score, url = lookup_rt(title, year)
                    if score:
                        entry.update(rt_score=score, rt_url=url)
                        changed = True
                if missing_desc:
                    desc = lookup_description(title, year)
                    if desc:
                        entry["description"] = desc
                        changed = True
                if entry:
                    entry.setdefault("title", title)
                    entry.setdefault("year", year)
                    cache[k] = entry
                log.info("    %s (%s): RT %s, %s", title, year,
                         entry.get("rt_score") or "n/a",
                         "description" if entry.get("description")
                         else "no description")
            seen[k] = entry
        entry = seen[k]
        m["rt_score"] = entry.get("rt_score") or ""
        m["rt_url"] = entry.get("rt_url") or ""
        m["description"] = entry.get("description") or ""
    if changed:
        try:
            save_cache(cache)
        except OSError as e:
            log.warning("  Couldn't write movie cache: %s", e)
    return movies
