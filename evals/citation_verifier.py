"""
evals/citation_verifier.py

Deterministic citation verification: an LLM never decides
whether a citation is correct. Two backends, same scoring:

  http    (default on a normal network)  GET the URL; it must resolve (2xx/3xx) AND
          the page text must contain enough of the claim's keywords.
  search  (fallback when the network blocks direct fetches, e.g. an allowlisted
          sandbox)  A haiku seat runs WebSearch for each claim and copies back the
          raw result URLs and snippets verbatim. Our code, not the model, decides
          whether the cited URL is among the results and whether the snippet
          supports the claim keywords.

`auto` (hybrid) fetches every URL over http, then re-checks through WebSearch any URL
the network refused (blocked CONNECT or proxy 403), so egress limits never shrink
the denominator silently.

Status per citation: verified | weak_support | not_found (URL absent from search) |
unresolved (HTTP error / no results) | network_blocked.
citation_accuracy = verified / (total - network_blocked)
url_exists_rate   = (verified + weak_support) / (total - network_blocked)
"""
from __future__ import annotations

import concurrent.futures as cf
import json
import os
import re
from dataclasses import asdict, dataclass, field
from urllib.parse import urlparse

STOP = set("""a an the and or of to in on for with by from at as is are was were be been being this that these those it its
into over under than then there their they them you your we our can could should would will may might more most less
using use used also such like via per not no yes how what when where which who why all any each other some only very""".split())
LINK = re.compile(r"\[([^\]]{1,200})\]\((https?://[^\s)]+)\)")
PAREN = re.compile(r"(?<!\])\((https?://[^\s)]+)\)")


@dataclass
class Citation:
    url: str
    claim: str
    keywords: list[str]
    status: str = "pending"
    http_status: int | None = None
    matched: list[str] = field(default_factory=list)
    backend: str = ""
    note: str = ""


def keywords(text: str, n: int = 8) -> list[str]:
    words = re.findall(r"[A-Za-z][A-Za-z0-9.+#-]{2,}|\d+(?:\.\d+)?%?", text)
    seen, out = set(), []
    for w in words:
        lw = w.lower().strip(".")
        if lw in STOP or (len(lw) < 4 and not any(ch.isdigit() for ch in lw)) or (lw.isdigit() and len(lw) < 2):
            continue
        if lw not in seen:
            seen.add(lw)
            out.append(lw)
    # prefer numbers and rarer (longer) words
    out.sort(key=lambda w: (not any(c.isdigit() for c in w), -len(w)))
    return out[:n]


def extract(markdown: str) -> list[Citation]:
    """Each linked URL with the sentence around it as the claim. Deduplicated by URL+claim."""
    cites, seen = [], set()
    body = markdown.split("\n## Sources")[0]  # the Sources list itself is not a claim
    for para in re.split(r"\n\s*\n", body):
        for sent in re.split(r"(?<=[.!?])\s+(?=[A-Z|*`-])", para):
            urls = [m.group(2) for m in LINK.finditer(sent)] + [m.group(1) for m in PAREN.finditer(sent)]
            clean = PAREN.sub("", LINK.sub(lambda x: x.group(1), sent)).strip(" -*|#>")
            for url in urls:
                url = url.rstrip(".,;:")
                claim = clean
                key = (url, claim[:80])
                if key in seen or len(claim) < 25:
                    continue
                seen.add(key)
                cites.append(Citation(url=url, claim=claim[:400], keywords=keywords(claim)))
    return cites


def _support(text: str, kws: list[str]) -> tuple[bool, list[str]]:
    t = text.lower()
    hit = [k for k in kws if k in t]
    need = max(2, int(len(kws) * 0.4 + 0.5)) if kws else 1
    return len(hit) >= min(need, len(kws) or 1), hit


# ----------------------------------------------------------------- http backend
def _http_one(c: Citation, timeout: float = 12.0) -> Citation:
    import httpx
    c.backend = "http"
    try:
        with httpx.Client(follow_redirects=True, timeout=timeout,
                          headers={"User-Agent": "Mozilla/5.0 (agent-fleet-harness citation verifier)"}) as cl:
            r = cl.get(c.url)
        c.http_status = r.status_code
        if r.status_code >= 400:
            c.status = "unresolved"
            return c
        text = re.sub(r"<[^>]+>", " ", r.text)
        ok, c.matched = _support(text, c.keywords)
        c.status = "verified" if ok else "weak_support"
    except (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout) as e:
        c.status, c.note = "network_blocked", type(e).__name__
    except Exception as e:  # noqa: BLE001
        c.status, c.note = "unresolved", f"{type(e).__name__}: {str(e)[:80]}"
    return c


# --------------------------------------------------------------- search backend
SEARCH_SCHEMA = {
    "type": "object", "required": ["results"],
    "properties": {"results": {"type": "array", "items": {"type": "object", "required": ["id", "hits"], "properties": {
        "id": {"type": "integer"},
        "hits": {"type": "array", "items": {"type": "object", "required": ["url"], "properties": {
            "url": {"type": "string"}, "title": {"type": "string"}, "snippet": {"type": "string"}}}}}}}},
}


def _norm(u: str) -> str:
    p = urlparse(u.strip())
    host = p.netloc.lower().removeprefix("www.")
    return f"{host}{p.path.rstrip('/')}".lower()


def _search_batch(cites: list[Citation], run_id: str) -> None:
    from agents.harness.claude_runner import run_seat
    items = "\n".join(f'{i}. claim: "{c.claim[:220]}"  |  cited: {c.url}' for i, c in enumerate(cites))
    r = run_seat(
        "citation-search", f"For each numbered item, run ONE WebSearch using the most specific words of the claim plus the cited site's domain. "
        f"Copy the top result URLs, titles and snippets back exactly as the search tool returned them. Do not judge anything.\n\n{items}",
        model="haiku", temperature=0.1, tools=["WebSearch"], json_schema=SEARCH_SCHEMA,
        system="You are a search clerk. You only run searches and copy results verbatim. Never fabricate URLs.",
        max_turns=len(cites) + 4, budget_usd=0.30 + 0.03 * len(cites), run_id=run_id)
    got = {x["id"]: x.get("hits", []) for x in (r.structured or {}).get("results", [])}
    for i, c in enumerate(cites):
        c.backend = "search"
        hits = got.get(i)
        if hits is None:
            c.status, c.note = "unresolved", r.error or "no search results returned"
            continue
        target = _norm(c.url)
        match = next((h for h in hits if _norm(h.get("url", "")) == target
                      or _norm(h.get("url", "")).startswith(target) or target.startswith(_norm(h.get("url", "")) + "/")), None)
        if not match:
            same_host = [h for h in hits if urlparse(h.get("url", "")).netloc.removeprefix("www.") == urlparse(c.url).netloc.removeprefix("www.")]
            c.status, c.note = "not_found", "cited URL not in search results" + (" (same site found)" if same_host else "")
            continue
        ok, c.matched = _support(f"{match.get('title','')} {match.get('snippet','')}", c.keywords)
        c.status = "verified" if ok else "weak_support"


# ------------------------------------------------------------------- entry point
def verify(markdown: str, *, backend: str = "auto", sample: int | None = 40, run_id: str = "",
           seen_urls: set[str] | None = None) -> dict:
    cites = extract(markdown)
    total_found = len(cites)
    if sample and len(cites) > sample:  # deterministic, spread across the document
        step = len(cites) / sample
        cites = [cites[int(i * step)] for i in range(sample)]
    used = backend
    if backend in ("auto", "http"):
        with cf.ThreadPoolExecutor(8) as ex:
            list(ex.map(_http_one, cites))
        used = "http"
        if backend == "auto":
            # behind an egress proxy, a blocked CONNECT or a proxy 403 says nothing about the source:
            # re-check those through WebSearch so the denominator is not silently shrunk
            redo = [c for c in cites if c.status == "network_blocked" or c.http_status in (403, 407)]
            if redo:
                used = "hybrid" if len(redo) < len(cites) else "search"
                for i in range(0, len(redo), 10):
                    _search_batch(redo[i:i + 10], run_id)
    elif backend == "search":
        for i in range(0, len(cites), 10):
            _search_batch(cites[i:i + 10], run_id)
    provenance = None
    if seen_urls is not None:
        seen = {_norm(u) for u in seen_urls}
        provenance = round(sum(_norm(c.url) in seen for c in cites) / len(cites), 3) if cites else None
    counted = [c for c in cites if c.status != "network_blocked"]
    verified = sum(c.status == "verified" for c in counted)
    return {
        "backend": used, "citations_found": total_found, "checked": len(cites),
        "verified": verified, "weak_support": sum(c.status == "weak_support" for c in counted),
        "unresolved": sum(c.status == "unresolved" for c in counted),
        "not_found": sum(c.status == "not_found" for c in counted),
        "url_exists_rate": round(sum(c.status in ("verified", "weak_support") for c in counted) / len(counted), 3) if counted else None,
        "network_blocked": len(cites) - len(counted),
        "citation_accuracy": round(verified / len(counted), 3) if counted else None,
        "provenance_rate": provenance,
        "unique_domains": len({urlparse(c.url).netloc for c in cites}),
        "details": [asdict(c) for c in cites],
    }


if __name__ == "__main__":
    import sys
    print(json.dumps({k: v for k, v in verify(open(sys.argv[1]).read(), backend=sys.argv[2] if len(sys.argv) > 2 else "auto").items() if k != "details"}, indent=2))
