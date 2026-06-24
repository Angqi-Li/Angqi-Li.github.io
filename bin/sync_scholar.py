#!/usr/bin/env python3
"""Sync new Google Scholar publications into the al-folio site.

For every paper on the configured Google Scholar profile that is not yet in
``_bibliography/papers.bib`` this script:

  * adds a BibTeX entry (enriched with DOI / venue / pages via Crossref),
  * renders a cover thumbnail from the paper's first PDF page (arXiv / Unpaywall
    open-access PDF), falling back to a generated title card,
  * writes a ``_news/announcement_<N>.md`` announcement.

It is meant to run from a scheduled GitHub Action that opens a pull request with
the result, but it also runs locally. Nothing is written with ``--dry-run``.

Env:
  SERPAPI_KEY   required unless --dry-run with --skip-fetch; the SerpAPI key.

Usage:
  SERPAPI_KEY=xxxx python bin/sync_scholar.py [--dry-run] [--verbose]
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import os
import re
import sys
from pathlib import Path
from typing import Optional

import requests

try:
    from rapidfuzz import fuzz
except ImportError:  # pragma: no cover - dependency hint
    print("Missing dependency 'rapidfuzz'. Run: pip install -r bin/requirements-scholar.txt", file=sys.stderr)
    raise

REPO_ROOT = Path(__file__).resolve().parent.parent
SOCIALS_YML = REPO_ROOT / "_data" / "socials.yml"
BIB_PATH = REPO_ROOT / "_bibliography" / "papers.bib"
NEWS_DIR = REPO_ROOT / "_news"
PREVIEW_DIR = REPO_ROOT / "assets" / "img" / "publication_preview"

SERPAPI_ENDPOINT = "https://serpapi.com/search.json"
CROSSREF_ENDPOINT = "https://api.crossref.org/works"
UNPAYWALL_ENDPOINT = "https://api.unpaywall.org/v2/"
ARXIV_ENDPOINT = "http://export.arxiv.org/api/query"

HTTP_TIMEOUT = 30
TITLE_MATCH_THRESHOLD = 88  # fuzzy score above which two titles are "the same"
PREVIEW_WIDTH = 600  # px width of the rendered cover thumbnail

# Words skipped when building the trailing word of a BibTeX key.
KEY_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "for", "to", "in", "on", "with",
    "using", "via", "based", "from", "by", "at", "is", "are",
}


# --------------------------------------------------------------------------- #
# small helpers
# --------------------------------------------------------------------------- #
def log(msg: str) -> None:
    print(msg, flush=True)


def vlog(verbose: bool, msg: str) -> None:
    if verbose:
        print(msg, flush=True)


def normalize_title(title: str) -> str:
    """Lowercase, strip punctuation and collapse whitespace for comparison."""
    title = re.sub(r"[^a-z0-9 ]+", " ", title.lower())
    return re.sub(r"\s+", " ", title).strip()


def read_yaml_scalar(path: Path, key: str) -> Optional[str]:
    """Tiny YAML reader for a top-level ``key: value`` line (no PyYAML dep).

    Handles inline ``# comments`` and an optional ``#fragment`` glued onto the
    value (e.g. ``gNeoT5QAAAAJ#d=gs_hdr_drw``)."""
    if not path.exists():
        return None
    pattern = re.compile(rf"^\s*{re.escape(key)}\s*:\s*(.+?)\s*$")
    for line in path.read_text(encoding="utf-8").splitlines():
        m = pattern.match(line)
        if not m:
            continue
        value = m.group(1)
        # drop an inline comment that is preceded by whitespace
        value = re.split(r"\s+#", value, maxsplit=1)[0].strip()
        return value
    return None


def get_scholar_id() -> str:
    raw = read_yaml_scalar(SOCIALS_YML, "scholar_userid")
    if not raw:
        raise SystemExit(f"Could not find 'scholar_userid' in {SOCIALS_YML}")
    return raw.split("#", 1)[0].strip()


def get_contact_email() -> str:
    return read_yaml_scalar(SOCIALS_YML, "email") or "noreply@example.com"


# --------------------------------------------------------------------------- #
# existing bibliography
# --------------------------------------------------------------------------- #
def load_existing(bib_text: str) -> tuple[set[str], set[str]]:
    """Return (normalized existing titles, existing bibtex keys)."""
    titles = {
        normalize_title(m.group(1))
        for m in re.finditer(r"(?<![a-zA-Z])title\s*=\s*\{(.+?)\}", bib_text, re.IGNORECASE | re.DOTALL)
    }
    keys = {m.group(1) for m in re.finditer(r"@\w+\{([^,\s]+)\s*,", bib_text)}
    return titles, keys


def title_is_known(title: str, existing_titles: set[str]) -> bool:
    norm = normalize_title(title)
    if norm in existing_titles:
        return True
    return any(fuzz.ratio(norm, t) >= TITLE_MATCH_THRESHOLD for t in existing_titles)


# --------------------------------------------------------------------------- #
# SerpAPI: Google Scholar author articles
# --------------------------------------------------------------------------- #
def fetch_scholar_articles(author_id: str, api_key: str, verbose: bool) -> list[dict]:
    articles: list[dict] = []
    start = 0
    while True:
        params = {
            "engine": "google_scholar_author",
            "author_id": author_id,
            "api_key": api_key,
            "num": 100,
            "start": start,
            "sort": "pubdate",
        }
        resp = requests.get(SERPAPI_ENDPOINT, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
        data = resp.json()
        if data.get("error"):
            raise SystemExit(f"SerpAPI error: {data['error']}")
        batch = data.get("articles", [])
        articles.extend(batch)
        vlog(verbose, f"  fetched {len(batch)} articles (start={start})")
        next_link = (data.get("serpapi_pagination") or {}).get("next")
        if not batch or not next_link:
            break
        start += len(batch)
    return articles


# --------------------------------------------------------------------------- #
# metadata enrichment
# --------------------------------------------------------------------------- #
def crossref_lookup(title: str, year: Optional[str], verbose: bool) -> Optional[dict]:
    try:
        params = {"query.bibliographic": title, "rows": 5}
        resp = requests.get(
            CROSSREF_ENDPOINT,
            params=params,
            timeout=HTTP_TIMEOUT,
            headers={"User-Agent": "al-folio-scholar-sync/1.0 (mailto:%s)" % get_contact_email()},
        )
        resp.raise_for_status()
        items = resp.json().get("message", {}).get("items", [])
    except Exception as exc:  # network / json issues should not abort the run
        vlog(verbose, f"    crossref lookup failed: {exc}")
        return None

    norm_target = normalize_title(title)
    best, best_score = None, 0.0
    for item in items:
        cand_title = (item.get("title") or [""])[0]
        score = fuzz.ratio(norm_target, normalize_title(cand_title))
        if score > best_score:
            best, best_score = item, score
    if best and best_score >= TITLE_MATCH_THRESHOLD:
        vlog(verbose, f"    crossref matched (score={best_score:.0f}) doi={best.get('DOI')}")
        return best
    return None


def arxiv_lookup(title: str, verbose: bool) -> Optional[str]:
    """Return an arXiv id if the title matches an arXiv paper, else None."""
    try:
        params = {"search_query": f'ti:"{title}"', "max_results": 3}
        resp = requests.get(ARXIV_ENDPOINT, params=params, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        vlog(verbose, f"    arxiv lookup failed: {exc}")
        return None

    norm_target = normalize_title(title)
    entries = re.findall(r"<entry>(.*?)</entry>", resp.text, re.DOTALL)
    for entry in entries:
        tmatch = re.search(r"<title>(.*?)</title>", entry, re.DOTALL)
        idmatch = re.search(r"<id>http[s]?://arxiv\.org/abs/([^<]+)</id>", entry)
        if not tmatch or not idmatch:
            continue
        if fuzz.ratio(norm_target, normalize_title(tmatch.group(1))) >= TITLE_MATCH_THRESHOLD:
            arxiv_id = idmatch.group(1).strip()
            vlog(verbose, f"    arxiv matched id={arxiv_id}")
            return arxiv_id
    return None


def arxiv_metadata(arxiv_id: str, verbose: bool) -> dict:
    """Fetch canonical title + full author list for an arXiv id."""
    try:
        resp = requests.get(ARXIV_ENDPOINT, params={"id_list": arxiv_id, "max_results": 1}, timeout=HTTP_TIMEOUT)
        resp.raise_for_status()
    except Exception as exc:
        vlog(verbose, f"    arxiv metadata fetch failed: {exc}")
        return {}
    entry = re.search(r"<entry>(.*?)</entry>", resp.text, re.DOTALL)
    if not entry:
        return {}
    body = entry.group(1)
    tmatch = re.search(r"<title>(.*?)</title>", body, re.DOTALL)
    names = [re.sub(r"\s+", " ", n).strip() for n in re.findall(r"<name>(.*?)</name>", body, re.DOTALL)]
    out = {"authors": names}
    if tmatch:
        out["title"] = re.sub(r"\s+", " ", tmatch.group(1)).strip()
    vlog(verbose, f"    arxiv metadata: {len(names)} authors")
    return out


def unpaywall_pdf_url(doi: str, email: str, verbose: bool) -> Optional[str]:
    try:
        resp = requests.get(f"{UNPAYWALL_ENDPOINT}{doi}", params={"email": email}, timeout=HTTP_TIMEOUT)
        if resp.status_code != 200:
            return None
        loc = resp.json().get("best_oa_location") or {}
        url = loc.get("url_for_pdf")
        if url:
            vlog(verbose, f"    unpaywall OA pdf: {url}")
        return url
    except Exception as exc:
        vlog(verbose, f"    unpaywall lookup failed: {exc}")
        return None


# --------------------------------------------------------------------------- #
# author / key formatting
# --------------------------------------------------------------------------- #
def authors_from_crossref(item: dict) -> Optional[str]:
    authors = item.get("author")
    if not authors:
        return None
    parts = []
    for a in authors:
        family = (a.get("family") or "").strip()
        given = (a.get("given") or "").strip()
        if family and given:
            parts.append(f"{family}, {given}")
        elif family:
            parts.append(family)
        elif a.get("name"):
            parts.append(a["name"].strip())
    return " and ".join(parts) if parts else None


def authors_from_string(authors: str) -> str:
    """Convert SerpAPI's 'S Gautam, A Li, S Ravishankar' to BibTeX form.

    SerpAPI truncates long author lists with a trailing '...'; those markers are
    dropped rather than emitted as a literal author."""
    out = []
    for chunk in authors.split(","):
        chunk = chunk.strip().strip(".…").strip()
        tokens = chunk.split()
        if not tokens:
            continue
        if len(tokens) == 1:
            out.append(tokens[0])
        else:
            family = tokens[-1]
            given = " ".join(tokens[:-1])
            out.append(f"{family}, {given}")
    return " and ".join(out)


def authors_from_arxiv(names: list[str]) -> Optional[str]:
    """Convert arXiv 'Angqi Li' style names to BibTeX 'Li, Angqi' form."""
    parts = []
    for name in names:
        tokens = name.strip().split()
        if not tokens:
            continue
        if len(tokens) == 1:
            parts.append(tokens[0])
        else:
            parts.append(f"{tokens[-1]}, {' '.join(tokens[:-1])}")
    return " and ".join(parts) if parts else None


def extract_arxiv_id(text: str) -> Optional[str]:
    """Pull an arXiv id (e.g. 2501.09799) out of a free-text string."""
    if not text:
        return None
    m = re.search(r"arxiv[:\s]*?(\d{4}\.\d{4,5})", text, re.IGNORECASE)
    return m.group(1) if m else None


def first_author_family(author_bibtex: str) -> str:
    first = author_bibtex.split(" and ")[0]
    family = first.split(",")[0] if "," in first else first.split()[-1]
    return re.sub(r"[^a-z]", "", family.lower()) or "anon"


def make_key(author_bibtex: str, year: str, title: str, used: set[str]) -> str:
    family = first_author_family(author_bibtex)
    first_word = "paper"
    for w in re.findall(r"[a-zA-Z]+", title.lower()):
        if w not in KEY_STOPWORDS:
            first_word = w
            break
    base = f"{family}{year}{first_word}"
    key = base
    suffix = ord("a")
    while key in used:
        key = f"{base}{chr(suffix)}"
        suffix += 1
    used.add(key)
    return key


# --------------------------------------------------------------------------- #
# cover image
# --------------------------------------------------------------------------- #
def render_pdf_cover(pdf_url: str, dest: Path, verbose: bool) -> bool:
    try:
        import fitz  # PyMuPDF
    except ImportError:
        vlog(verbose, "    PyMuPDF not installed; skipping PDF render")
        return False
    try:
        resp = requests.get(pdf_url, timeout=HTTP_TIMEOUT, headers={"User-Agent": "Mozilla/5.0"})
        resp.raise_for_status()
        if "pdf" not in resp.headers.get("Content-Type", "") and not resp.content[:4] == b"%PDF":
            vlog(verbose, "    download was not a PDF")
            return False
        doc = fitz.open(stream=resp.content, filetype="pdf")
        page = doc.load_page(0)
        scale = PREVIEW_WIDTH / page.rect.width
        pix = page.get_pixmap(matrix=fitz.Matrix(scale, scale))
        dest.parent.mkdir(parents=True, exist_ok=True)
        pix.save(str(dest))
        vlog(verbose, f"    rendered cover from PDF -> {dest.name}")
        return True
    except Exception as exc:
        vlog(verbose, f"    pdf render failed: {exc}")
        return False


def render_placeholder_cover(title: str, dest: Path, verbose: bool) -> bool:
    try:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
        import textwrap
    except ImportError:
        vlog(verbose, "    matplotlib not installed; cannot make placeholder")
        return False
    try:
        fig = plt.figure(figsize=(6, 4.5), dpi=100)
        fig.patch.set_facecolor("#1f2d3d")
        ax = fig.add_axes([0, 0, 1, 1])
        ax.axis("off")
        wrapped = "\n".join(textwrap.wrap(title, width=28)[:6])
        ax.text(0.5, 0.5, wrapped, color="white", ha="center", va="center",
                fontsize=18, weight="bold")
        dest.parent.mkdir(parents=True, exist_ok=True)
        fig.savefig(str(dest), facecolor=fig.get_facecolor())
        plt.close(fig)
        vlog(verbose, f"    generated placeholder cover -> {dest.name}")
        return True
    except Exception as exc:
        vlog(verbose, f"    placeholder render failed: {exc}")
        return False


# --------------------------------------------------------------------------- #
# entry / news building
# --------------------------------------------------------------------------- #
def build_entry(article: dict, key: str, meta: dict, preview_name: Optional[str]) -> str:
    fields = [("title", meta["title"]), ("author", meta["author"])]
    if meta.get("entry_type") == "inproceedings":
        fields.append(("booktitle", meta["venue"]))
    elif meta.get("venue"):
        fields.append(("journal", meta["venue"]))
    if meta.get("pages"):
        fields.append(("pages", meta["pages"]))
    fields.append(("year", meta["year"]))
    if meta.get("doi"):
        fields.append(("doi", meta["doi"]))
    if meta.get("pdf"):
        fields.append(("pdf", meta["pdf"]))
    if meta.get("url"):
        fields.append(("url", meta["url"]))
    if preview_name:
        fields.append(("preview", preview_name))

    lines = [f"@{meta.get('entry_type', 'article')}{{{key},"]
    for i, (name, value) in enumerate(fields):
        comma = "," if i < len(fields) - 1 else ""
        lines.append(f"  {name}={{{value}}}{comma}")
    lines.append("}")
    return "\n".join(lines)


def next_news_index() -> int:
    max_n = 0
    for f in NEWS_DIR.glob("announcement_*.md"):
        m = re.match(r"announcement_(\d+)\.md", f.name)
        if m:
            max_n = max(max_n, int(m.group(1)))
    return max_n + 1


def build_news(title: str, url: str, date: dt.date) -> str:
    stamp = f"{date.year}-{date.month}-{date.day} 15:59:00-0400"
    return (
        "---\n"
        "layout: post\n"
        f"date: {stamp}\n"
        "inline: true\n"
        "related_posts: false\n"
        "---\n\n"
        f"New Publication Announcement: {title}: [link]({url})\n"
    )


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def resolve_metadata(article: dict, verbose: bool, email: str) -> dict:
    """Combine SerpAPI + Crossref + arXiv/Unpaywall into a single metadata dict."""
    title = (article.get("title") or "").strip()
    serp_year = (article.get("year") or "").strip()
    scholar_link = article.get("link") or ""

    publication = article.get("publication") or ""

    cr = crossref_lookup(title, serp_year, verbose)
    doi = cr.get("DOI") if cr else None

    # arXiv id: prefer the one SerpAPI already reports in the publication string
    # (reliable for brand-new papers), else fall back to a title search.
    arxiv_id = extract_arxiv_id(publication) or extract_arxiv_id(scholar_link)
    if not arxiv_id and not doi:
        arxiv_id = arxiv_lookup(title, verbose)

    # arXiv metadata gives the full author list + canonical title when Crossref
    # has no (good) match — much better than SerpAPI's abbreviated/truncated form.
    arxiv_meta = arxiv_metadata(arxiv_id, verbose) if (arxiv_id and not cr) else {}

    # title (prefer canonical capitalization from Crossref, then arXiv)
    cr_title = (cr.get("title") or [None])[0] if cr else None
    title_out = cr_title or arxiv_meta.get("title") or title

    # authors (Crossref > arXiv > SerpAPI string)
    author = (
        (cr and authors_from_crossref(cr))
        or authors_from_arxiv(arxiv_meta.get("authors", []))
        or authors_from_string(article.get("authors", ""))
        or "Unknown"
    )

    # year
    year = serp_year
    if cr:
        try:
            year = str((cr.get("issued") or cr.get("published") or {})["date-parts"][0][0])
        except Exception:
            pass

    # venue / entry type
    venue = (cr.get("container-title") or [None])[0] if cr else None
    cr_type = (cr or {}).get("type", "")
    entry_type = "inproceedings" if "proceedings" in cr_type else "article"
    if not venue:
        venue = publication.split(",")[0].strip() or None
    if arxiv_id and not doi:
        venue = f"arXiv preprint arXiv:{arxiv_id}"
        entry_type = "article"

    pages = cr.get("page") if cr else None

    # canonical link (doi > arxiv abs > scholar as last resort)
    if doi:
        url = f"https://doi.org/{doi}"
    elif arxiv_id:
        url = f"https://arxiv.org/abs/{arxiv_id}"
    else:
        url = scholar_link

    # ordered list of PDF urls to try when rendering the cover thumbnail
    pdf_candidates: list[str] = []
    if arxiv_id:
        pdf_candidates.append(f"https://arxiv.org/pdf/{arxiv_id}")
    if doi:
        oa = unpaywall_pdf_url(doi, email, verbose)
        if oa:
            pdf_candidates.append(oa)
        # bioRxiv / medRxiv expose a predictable full-text PDF url
        if cr and any("rxiv" in (p or "").lower() for p in (cr.get("container-title") or []) + [cr.get("publisher", "")]):
            pdf_candidates.append(f"https://www.biorxiv.org/content/{doi}v1.full.pdf")
        for link in (cr.get("link") or []):
            if link.get("content-type") == "application/pdf" and link.get("URL"):
                pdf_candidates.append(link["URL"])

    pdf_link = pdf_candidates[0] if pdf_candidates else url

    return {
        "title": title_out,
        "author": author,
        "venue": venue,
        "entry_type": entry_type,
        "year": year or "n.d.",
        "doi": doi,
        "pages": pages,
        "pdf": pdf_link,
        "url": url,
        "pdf_candidates": pdf_candidates,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Sync Google Scholar -> publications + news")
    parser.add_argument("--dry-run", action="store_true", help="print changes, write nothing")
    parser.add_argument("--verbose", "-v", action="store_true", help="verbose logging")
    args = parser.parse_args()

    api_key = os.environ.get("SERPAPI_KEY", "").strip()
    if not api_key:
        raise SystemExit("SERPAPI_KEY environment variable is not set.")

    author_id = get_scholar_id()
    email = get_contact_email()
    log(f"Scholar author id: {author_id}")

    bib_text = BIB_PATH.read_text(encoding="utf-8") if BIB_PATH.exists() else ""
    existing_titles, existing_keys = load_existing(bib_text)
    log(f"Existing publications in papers.bib: {len(existing_titles)}")

    articles = fetch_scholar_articles(author_id, api_key, args.verbose)
    log(f"Articles on Scholar profile: {len(articles)}")

    new_articles = [a for a in articles if a.get("title") and not title_is_known(a["title"], existing_titles)]
    if not new_articles:
        log("No new publications. Nothing to do. ✅")
        return 0
    log(f"New publications to add: {len(new_articles)}")

    used_keys = set(existing_keys)
    news_index = next_news_index()
    today = dt.date.today()
    new_entries: list[str] = []

    for article in new_articles:
        log(f"\n• {article.get('title')}")
        meta = resolve_metadata(article, args.verbose, email)
        key = make_key(meta["author"], str(meta["year"]), meta["title"], used_keys)
        log(f"  key={key}  doi={meta.get('doi')}  venue={meta.get('venue')}")

        # cover image
        preview_name = f"{key}.png"
        dest = PREVIEW_DIR / preview_name
        if args.dry_run:
            cands = meta.get("pdf_candidates") or []
            log(f"  [dry-run] would render cover from {cands or 'placeholder'} -> {preview_name}")
        else:
            ok = False
            for pdf_url in meta.get("pdf_candidates", []):
                if render_pdf_cover(pdf_url, dest, args.verbose):
                    ok = True
                    break
            if not ok:
                ok = render_placeholder_cover(meta["title"], dest, args.verbose)
            if not ok:
                preview_name = None  # entry without a preview rather than a broken link

        entry = build_entry(article, key, meta, preview_name)
        new_entries.append(entry)
        log("  bib entry:\n" + "\n".join("    " + ln for ln in entry.splitlines()))

        # news file
        news_name = f"announcement_{news_index}.md"
        news_body = build_news(meta["title"], meta["url"], today)
        news_index += 1
        if args.dry_run:
            log(f"  [dry-run] would write _news/{news_name}")
        else:
            (NEWS_DIR / news_name).write_text(news_body, encoding="utf-8")
            log(f"  wrote _news/{news_name}")

    if args.dry_run:
        log("\n[dry-run] no files written.")
        return 0

    addition = "\n\n" + "\n\n".join(new_entries) + "\n"
    with BIB_PATH.open("a", encoding="utf-8") as fh:
        fh.write(addition)
    log(f"\nAppended {len(new_entries)} entr{'y' if len(new_entries) == 1 else 'ies'} to papers.bib ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
