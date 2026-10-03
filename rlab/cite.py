"""Kernel-side citation verification: a cited identifier must resolve and its title must match.

The previous check was a format regex only, so an invented PMID passed. Resolvers: PubMed and PMC
(NCBI E-utilities), DOI (Crossref, falling back to the doi.org handle service for DataCite and
other registries), arXiv. A resolver that cannot be reached (network error, rate limit, odd body)
leaves the source in place with `verified: None`; a source that resolves to nothing, or to a
clearly different title, is dropped. Nothing here may raise: a flaky network must never stall the
kernel or burn a paid worker call, so every failure path degrades to "unverified".

Titles are compared by containment of the shorter token set in the longer (crude plural
stemming), so a short claimed title matches a long real one, and titles with no comparable
tokens (CJK, very short) are accepted on the strength of the id alone (`verified: "id_only"`).
"""

from __future__ import annotations

import http.client
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Callable

UA = "rlab-cite/0.1"
MIN_INTERVAL_S = 0.34  # NCBI allows about 3 requests per second without a key
_FLAGS = re.I | re.A  # ASCII digits only: fullwidth or Arabic-Indic digits are not identifiers
_PMID = re.compile(r"^(?:PMID:?\s*(\d{1,9})|(\d{5,9}))$", _FLAGS)
_PMC = re.compile(r"^PMC(\d+)$", _FLAGS)
_DOI = re.compile(r"^(?:doi:\s*)?(10\.\d{4,9}/\S+)$", _FLAGS)
_ARXIV = re.compile(r"^(?:arxiv:\s*)?(\d{4}\.\d{4,5})(?:v\d+)?$", _FLAGS)
ID_FORMAT = re.compile(r"^(?:PMID:?\s*\d{1,9}|\d{5,9}|PMC\d+|(?:doi:\s*)?10\.\d{4,9}/\S+|"
                       r"(?:arxiv:\s*)?\d{4}\.\d{4,5}(?:v\d+)?)$", _FLAGS)
MIN_TITLE_OVERLAP = 0.5

Http = Callable[[str], tuple[int, str]]
_last_call = [0.0]


def http_get(url: str, timeout: float = 15.0) -> tuple[int, str]:
    wait = MIN_INTERVAL_S - (time.monotonic() - _last_call[0])
    if wait > 0:
        time.sleep(wait)
    _last_call[0] = time.monotonic()
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""
    except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException, ValueError):
        return 0, ""


def _stem(t: str) -> str:
    return t[:-1] if len(t) > 4 and t.endswith("s") and not t.endswith("ss") else t


def _tokens(s) -> set[str]:
    if not isinstance(s, str):
        return set()
    return {_stem(t) for t in re.findall(r"[a-z0-9]+", s.lower()) if len(t) >= 3}


def title_overlap(a, b) -> float | None:
    """Containment of the shorter token set in the longer; None when the titles are not comparable."""
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return None
    return len(ta & tb) / min(len(ta), len(tb))


def _json(body: str):
    try:
        return json.loads(body)
    except ValueError:
        return None


def _unknown() -> dict:
    return {"exists": None, "title": ""}


def _esummary(db: str, uid: str, http: Http) -> dict:
    code, body = http(f"https://eutils.ncbi.nlm.nih.gov/entrez/eutils/esummary.fcgi?db={db}&retmode=json&id={uid}")
    data = _json(body) if code == 200 else None
    if not isinstance(data, dict) or "error" in data or not isinstance(data.get("result"), dict):
        return _unknown()  # unreachable, rate limited, or an unexpected body: do not call it nonexistent
    rec = data["result"].get(uid)
    if not isinstance(rec, dict) or "error" in rec or not rec.get("title"):
        return {"exists": False, "title": ""}
    return {"exists": True, "title": rec["title"] if isinstance(rec["title"], str) else ""}


def _resolve(cid: str, http: Http) -> dict:
    if m := _PMID.match(cid):
        return _esummary("pubmed", str(int(m.group(1) or m.group(2))), http)
    if m := _PMC.match(cid):
        return _esummary("pmc", str(int(m.group(1))), http)
    if m := _DOI.match(cid):
        doi = m.group(1).rstrip(".,;)]")
        code, body = http("https://api.crossref.org/works/" + urllib.parse.quote(doi))
        if code == 200:
            data = _json(body)
            msg = data.get("message") if isinstance(data, dict) else None
            if not isinstance(msg, dict):
                return _unknown()
            titles = msg.get("title")
            title = titles[0] if isinstance(titles, list) and titles and isinstance(titles[0], str) else ""
            return {"exists": True, "title": title}
        if code == 404:  # not a Crossref DOI; DataCite (Zenodo, Dryad, ...) and others live elsewhere
            code2, body2 = http("https://doi.org/api/handles/" + urllib.parse.quote(doi))
            data = _json(body2) if code2 in (200, 404) else None
            rc = data.get("responseCode") if isinstance(data, dict) else None
            if rc == 1:
                return {"exists": True, "title": ""}
            if rc == 100 or code2 == 404:
                return {"exists": False, "title": ""}
        return _unknown()
    if m := _ARXIV.match(cid):
        code, body = http("https://export.arxiv.org/api/query?id_list=" + m.group(1))
        if code != 200:
            return _unknown()
        entry = re.search(r"<entry>.*?<title>(.*?)</title>", body, re.S)
        title = re.sub(r"\s+", " ", entry.group(1)).strip() if entry else ""
        return {"exists": bool(entry) and title.lower() != "error", "title": title}
    return {"exists": False, "title": ""}


def resolve(cid, http: Http = http_get) -> dict:
    """{'exists': True|False|None, 'title': str}; None means the resolver could not decide."""
    try:
        return _resolve(str(cid).strip(), http)
    except Exception:  # a resolver quirk must never crash the kernel
        return _unknown()


def verify_sources(sources: list[dict], http: Http = http_get) -> tuple[list[dict], list[dict]]:
    """Split sources into (kept, rejected). Kept sources gain `verified`: True (id and title agree),
    "id_only" (id resolves, titles not comparable) or None (resolver could not decide)."""
    kept, rejected = [], []
    for s in sources:
        r = resolve(s.get("id", ""), http)
        if r["exists"] is False:
            rejected.append({**s, "reject_reason": "identifier does not resolve"})
            continue
        if r["exists"] is None:
            kept.append({**s, "verified": None})
            continue
        overlap = title_overlap(s.get("title"), r["title"])
        if overlap is None:
            kept.append({**s, "verified": "id_only"})
        elif overlap < MIN_TITLE_OVERLAP:
            rejected.append({**s, "reject_reason": f"title mismatch: resolver says '{r['title'][:80]}'"})
        else:
            kept.append({**s, "verified": True})
    return kept, rejected
