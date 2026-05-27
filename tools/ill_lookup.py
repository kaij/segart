"""ILL lookup library: given a citation (issn, vol, iss, year, pages,
title, author), pick the IA item + start/end page-index and report a
calibrated confidence.

Strategies (run in priority order; first to reach conf ≥ 90 short-circuits):

  toc        — if `<item>_toc.json` exists, match title against TOC entries
                and return the structured page_index_range.  Dormant today
                (only ~149 published TOCs) but wired up for the future.
  docling    — if `<item>_docling.json.gz` exists, locate the article's
                start by finding a section_header / title whose tokens
                overlap the requested title; end by walking forward to the
                next section_header / "References".
  heuristic  — printed→leaf via scandata assertions + pn.json, content
                confirmation via hOCR searchtext, forward-scan end signal.
                The v4 logic from /tmp/ill_lookup_v4.py, lifted in.

Read-only.
"""
from __future__ import annotations

import gzip
import json
import re
import subprocess
import sys
import time
import unicodedata
import xml.etree.ElementTree as ET
from collections import Counter, deque
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

SEGART = Path("/Users/brewster/tmp/segart")
DEFAULT_CACHE = SEGART / "tmp" / "items"

sys.path.insert(0, str(SEGART))
from page_index import PageIndex  # noqa: E402


# --------------------------------------------------------------------- types

@dataclass
class LookupRequest:
    issn: str
    vol: str = ""
    iss: str = ""
    yr: str = ""
    pages: str = ""        # patron-supplied page range string, e.g. "234-241"
    title: str = ""        # article title
    author: str = ""
    title_journal: str = ""  # journal title (for ISSN-less searches)


@dataclass
class LookupResult:
    picked_item: Optional[str] = None
    start: Optional[int] = None    # BR page-index integer
    end: Optional[int] = None
    strategy: str = ""             # "toc" | "docling" | "heuristic" | ""
    confidence: int = 0            # 0–100
    evidence: list = field(default_factory=list)
    error: str = ""


# --------------------------------------------------------------------- norm

REF_RE = re.compile(
    r"\b(references|bibliography|literature\s+cited|acknowledg(?:e?)ments?"
    r"|received\s+(?:\w+\s+)?\d{1,2},?\s+\d{4}"
    r"|received\s+\d{1,2}\s+\w+\s+\d{4})\b", re.IGNORECASE)

DIA = str.maketrans({
    "ß":"ss","ä":"ae","ö":"oe","ü":"ue","Ä":"Ae","Ö":"Oe","Ü":"Ue",
    "é":"e","è":"e","ê":"e","ë":"e","É":"E","á":"a","à":"a","â":"a",
    "í":"i","ì":"i","î":"i","ó":"o","ò":"o","ô":"o","ú":"u","ù":"u",
    "ñ":"n","ç":"c","Ñ":"N","Ç":"C",
    "—":"-","–":"-","‑":"-","\u2018":"'","\u2019":"'","\u201c":'"',"\u201d":'"',
})

def _norm(s: str) -> str:
    if not s: return ""
    s = unicodedata.normalize("NFKC", s).translate(DIA)
    s = unicodedata.normalize("NFKD", s)
    return "".join(c for c in s if not unicodedata.combining(c)).lower()

_TOK_RE = re.compile(r"[a-z0-9]+")
def _toks(s): return set(_TOK_RE.findall(_norm(s)))

STOP = {"the","of","in","and","on","a","an","for","to","with","by","or",
        "is","at","de","la","le","les","du","et","des","une","un","von",
        "der","die","das","im","zur","fur","bei","aus","mit","zu","sur",
        "sui","ein","eine","einer","over","som","its","as","be","from",
        "this","that","into","over","under","between","among"}

def _title_toks(title, minlen=4):
    return {t for t in _toks(title) if len(t) >= minlen and t not in STOP}


def parse_pages(s: str):
    """Return (start_int, end_int) or None.

    Handles the standard publishing convention where trailing digits are
    abbreviated:
        987-93     →  987–993
        671-85     →  671–685
        407-18     →  407–418
        1421-30    →  1421–1430
    Rule: when the end string has fewer digits than the start AND
    int(end_str) < int(start), expand by replacing the trailing digits
    of `start` with `end_str`.

    If only start is supplied ('69' or '69-'), end == start.
    """
    s = (s or "").strip()
    m = re.match(r"^(\d+)\s*[-\u2013]\s*(\d+)", s)
    if m:
        start_str, end_str = m.group(1), m.group(2)
        start_n, end_n = int(start_str), int(end_str)
        if len(end_str) < len(start_str) and end_n < start_n:
            # abbreviated: take leading digits of start
            scale = 10 ** len(end_str)
            base = (start_n // scale) * scale
            end_n = base + end_n
            # If we still landed below start (e.g. start=995, end=03 → 1003),
            # bump up by one decade.
            if end_n < start_n:
                end_n += scale
        return start_n, end_n
    m = re.match(r"^(\d+)", s)
    if m: return int(m.group(1)), int(m.group(1))
    return None


# --------------------------------------------------------------------- title→slug

_TITLE_INDEX_PATH = SEGART / "tmp" / "audit" / "title_to_pub_slug.json"
_title_index = None

def _norm_title(t):
    if not t: return ""
    t = t.lower().translate(DIA)
    t = re.sub(r"\s*\d{4}\s*[-\u2013]\s*\d{4}\s*$", "", t)
    t = re.sub(r"[^a-z0-9 ]", " ", t)
    return " ".join(t.split())

def _load_title_index():
    global _title_index
    if _title_index is None:
        try:
            _title_index = json.load(open(_TITLE_INDEX_PATH))
        except Exception:
            _title_index = {}
    return _title_index

def lookup_publication_collection(journal_title):
    """Return the IA publication-collection identifier (e.g. `pub_X`) for
    a given journal title.

    Priority:
      1. Authoritative IA `mediatype:collection` search on the title field.
      2. Historical ILL log index — catches abbreviated / informal titles
         that don't match the formal IA collection title (e.g. "Clin Orthop
         Relat Res" → sim_clinical-orthopaedics-and-related-research).
    """
    if not journal_title: return None
    title_clean = re.sub(r"\s*\d{4}\s*[-\u2013]\s*\d{4}\s*$", "", journal_title).strip()
    title_clean = re.sub(r"[.:;,]+$", "", title_clean).strip()
    if title_clean:
        try:
            r = subprocess.run(
                ["ia","search",
                 f'mediatype:collection AND collection:periodicals AND title:"{title_clean}"',
                 "--field=identifier","--field=title",
                 "--parameters","scope=all"],
                capture_output=True, text=True, timeout=15)
            hits = []
            for line in r.stdout.splitlines():
                try: d = json.loads(line)
                except: continue
                hits.append(d)
            target = title_clean.lower()
            for h in hits:
                if (h.get("title") or "").strip().lower() == target:
                    return h.get("identifier")
            if hits: return hits[0].get("identifier")
        except Exception:
            pass
    # Fallback: historical ILL log index for abbreviated / informal titles
    idx = _load_title_index()
    nt = _norm_title(journal_title)
    slugs = idx.get(nt)
    if slugs:
        def _is_pub_slug(s):
            return (s.startswith(("sim_","pub_"))
                    and not re.search(r"_(?:19|20)\d{2}", s)
                    and not re.search(r"_\d+_\d", s))
        pub_only = {k: v for k, v in slugs.items() if _is_pub_slug(k)}
        if pub_only:
            slug = max(pub_only.items(), key=lambda kv: kv[1])[0]
            # Translate sim_X → pub_X for the collection-membership query
            if slug.startswith("sim_"):
                return "pub_" + slug[4:]
            return slug
    return None


# Backwards-compat shim for code that still calls lookup_pub_slug
def lookup_pub_slug(journal_title):
    return lookup_publication_collection(journal_title)


# --------------------------------------------------------------------- ia

def _ia_search(q, retries=2):
    for i in range(retries+1):
        try:
            r = subprocess.run(["ia","search",q,
                "--field=identifier","--field=date","--field=volume","--field=issue",
                "--parameters","scope=all"],
                capture_output=True, text=True, timeout=30)
            return [json.loads(l) for l in r.stdout.splitlines() if l.strip()]
        except Exception:
            if i == retries: raise
            time.sleep(1 + i)
    return []


def search_candidates(req: LookupRequest):
    seen = set()
    tiers = {"T1": [], "T2": [], "T3": []}
    def push(tier, items):
        for i in items:
            ident = i.get("identifier")
            if not ident or ident in seen: continue
            if "index" in ident.lower() or "contents" in ident.lower(): continue
            seen.add(ident); tiers[tier].append(i)
    # Path A: ISSN-based (preferred; Rapid requests always have this)
    if req.issn and req.vol and req.iss:
        push("T1", _ia_search(f'issn:{req.issn} AND volume:"{req.vol}" AND issue:"{req.iss}"'))
    if req.issn and req.vol:
        push("T2", _ia_search(f'issn:{req.issn} AND volume:"{req.vol}"'))
    if req.issn and req.yr:
        push("T3", _ia_search(f"issn:{req.issn} AND date:{req.yr}*"))
    # Path B: journal_title → publication collection (for IDS-without-ISSN).
    # Find the pub_X collection via IA search, then narrow by collection.
    pub_collection = lookup_publication_collection(
        getattr(req, "title_journal", "") or req.title)
    if not tiers["T1"] and not tiers["T2"] and pub_collection:
        if req.vol and req.iss:
            push("T1", _ia_search(f'collection:"{pub_collection}" AND volume:"{req.vol}" AND issue:"{req.iss}"'))
        if req.vol and not tiers["T1"]:
            push("T2", _ia_search(f'collection:"{pub_collection}" AND volume:"{req.vol}"'))
        if req.yr and not tiers["T1"] and not tiers["T2"]:
            push("T3", _ia_search(f'collection:"{pub_collection}" AND date:{req.yr}*'))

    # Disambiguate via full-text search: if a tier returned multiple
    # candidates, search within the collection for a distinctive phrase
    # from the article title; reorder candidates so full-text hits come
    # first. The IA search engine indexes the per-item OCR text under the
    # default search field, so a quoted phrase narrows nicely.
    if req.title:
        phrase = _distinctive_phrase(req.title)
        if phrase and pub_collection:
            for tname in ("T1","T2","T3"):
                if len(tiers[tname]) <= 1: continue
                ft_q = f'collection:"{pub_collection}" AND "{phrase}"'
                if req.yr: ft_q += f" AND date:{req.yr}*"
                ft_hits = {h.get("identifier") for h in _ia_search(ft_q)}
                if ft_hits:
                    tiers[tname].sort(key=lambda c: 0 if c.get("identifier") in ft_hits else 1)
    return tiers


def _distinctive_phrase(title: str, max_words: int = 5) -> str:
    """Pick the first ~5 meaningful (non-stopword) consecutive words from
    the article title. Quoted in the IA search query for precision."""
    if not title: return ""
    words = re.findall(r"[A-Za-z][A-Za-z\-']{2,}", title)
    out = []
    for w in words:
        lw = w.lower()
        if lw in STOP: continue
        out.append(w)
        if len(out) >= max_words: break
    return " ".join(out)


def fetch_item_files(item, cache_dir=DEFAULT_CACHE):
    """Fetch the small per-item files we use. Skip ones already present."""
    dest = cache_dir / item
    dest.mkdir(parents=True, exist_ok=True)
    sufs = ("_scandata.xml", "_page_numbers.json",
            "_hocr_searchtext.txt.gz", "_hocr_pageindex.json.gz",
            "_docling.json.gz", "_toc.json")
    missing = [s for s in sufs if not (dest / f"{item}{s}").exists()]
    if missing:
        for attempt in range(2):
            subprocess.run(["ia","download",item,
                            "--glob","|".join(f"{item}{s}" for s in missing),
                            "--destdir",str(cache_dir),"--retries","1"],
                            capture_output=True, timeout=120)
            if all((dest / f"{item}{s}").exists() for s in missing): break
            time.sleep(1 + attempt)
    return {s: (dest / f"{item}{s}") if (dest / f"{item}{s}").exists() else None
            for s in sufs}


# --------------------------------------------------------------------- ctx

class ItemContext:
    """All per-item data we might need, lazily loaded."""
    def __init__(self, item, cache_dir=DEFAULT_CACHE):
        self.item = item
        self.paths = fetch_item_files(item, cache_dir)
        self._pi = None; self._asn = None; self._pn = None
        self._pix = None; self._text = None; self._dl = None; self._toc = None

    @property
    def pi(self):
        if self._pi is None:
            for i in range(3):
                try: self._pi = PageIndex.for_item(self.item, fetch=True); break
                except Exception as e:
                    if i == 2: raise
                    time.sleep(1 + i)
        return self._pi

    @property
    def n_leaves(self): return self.pi.visible_count or 0

    @property
    def assertions(self):
        if self._asn is None:
            out = {}
            sd = self.paths.get("_scandata.xml")
            if sd:
                try:
                    for a in ET.parse(sd).getroot().iter("assertion"):
                        l = a.find("leafNum"); p = a.find("pageNum")
                        if l is not None and l.text and p is not None:
                            out[int(l.text)] = (p.text or "").strip()
                except Exception: pass
            self._asn = out
        return self._asn

    @property
    def pn(self):
        if self._pn is None:
            p = self.paths.get("_page_numbers.json")
            self._pn = json.load(open(p)) if p else {"pages": []}
        return self._pn

    @property
    def printed_to_br(self):
        pmap = self.pi.printed_to_br(self.pn) if self.pn.get("pages") else {}
        for leaf, page in self.assertions.items():
            if not page: continue
            br = self.pi.scandata_to_br(leaf) or leaf
            pmap[page] = br
        return pmap

    @property
    def pix(self):
        if self._pix is None:
            p = self.paths.get("_hocr_pageindex.json.gz")
            self._pix = json.load(gzip.open(p)) if p else None
        return self._pix

    @property
    def text(self):
        if self._text is None:
            p = self.paths.get("_hocr_searchtext.txt.gz")
            self._text = gzip.open(p, "rb").read().decode("utf-8", errors="replace") if p else ""
        return self._text

    def leaf_text(self, leaf):
        if not self.pix or leaf < 0 or leaf >= len(self.pix): return ""
        s, e, _, _ = self.pix[leaf]
        return self.text[s:e]

    @property
    def docling(self):
        if self._dl is None:
            p = self.paths.get("_docling.json.gz")
            if p:
                try: self._dl = json.load(gzip.open(p, "rt"))
                except Exception: self._dl = None
        return self._dl

    @property
    def toc(self):
        if self._toc is None:
            p = self.paths.get("_toc.json")
            if p:
                try: self._toc = json.load(open(p))
                except Exception: self._toc = None
        return self._toc


# --------------------------------------------------------------------- helpers

def title_at_top_of_leaf(ctx, leaf, title):
    """True iff the article title appears densely at the top of the leaf."""
    tt = _title_toks(title)
    if len(tt) < 3: return False, 0.0, "title_too_short"
    text = ctx.leaf_text(leaf)
    if not text: return False, 0.0, "no_text"
    top = text[:max(300, len(text)//2)]
    nt = _norm(top)
    positions = []
    for tok in tt:
        for m in re.finditer(r"\b"+re.escape(tok)+r"\b", nt):
            positions.append((m.start(), tok))
    if not positions:
        whole_hits = tt & _toks(text)
        return False, len(whole_hits)/len(tt), f"no_top_match"
    positions.sort()
    cnt = Counter(); dq = deque(); best = 0
    for p, tok in positions:
        dq.append((p, tok)); cnt[tok] += 1
        while dq and dq[0][0] < p - 300:
            o_p, o_t = dq.popleft(); cnt[o_t] -= 1
            if cnt[o_t] == 0: del cnt[o_t]
        if len(cnt) > best: best = len(cnt)
    frac_top = best / len(tt)
    whole_hits = tt & _toks(text)
    frac_whole = len(whole_hits) / len(tt)
    if frac_top >= 0.6:
        return True, frac_top, f"top({frac_top:.0%})"
    if frac_whole >= 0.8 and frac_top >= 0.4:
        return True, frac_whole, f"whole({frac_whole:.0%})"
    return False, max(frac_top, frac_whole), f"insufficient(top={frac_top:.0%},whole={frac_whole:.0%})"


def find_end_by_hocr_scan(ctx, start_leaf_sd, current_title, max_scan=50):
    """Forward-scan from start_leaf for refs marker or next article boundary."""
    if start_leaf_sd is None or start_leaf_sd < 0: return None, "bad_start"
    current_tt = _title_toks(current_title)
    refs_leaf = None
    for offset in range(1, max_scan + 1):
        leaf = start_leaf_sd + offset
        if leaf >= ctx.n_leaves: break
        if REF_RE.search(ctx.leaf_text(leaf)):
            refs_leaf = leaf; break
    next_art_leaf = None
    if current_tt:
        for offset in range(2, max_scan + 1):
            leaf = start_leaf_sd + offset
            if leaf >= ctx.n_leaves: break
            text = ctx.leaf_text(leaf)
            if not text: continue
            top = text[:max(300, len(text)//3)]
            top_toks = _toks(top)
            our_frac = len(current_tt & top_toks) / max(1, len(current_tt))
            new_meaningful = {t for t in top_toks if len(t) >= 5 and t not in STOP} - current_tt
            if our_frac < 0.2 and len(new_meaningful) >= 4:
                next_art_leaf = leaf - 1; break
    candidates = []
    if refs_leaf is not None: candidates.append((refs_leaf, "refs"))
    if next_art_leaf is not None: candidates.append((next_art_leaf, "next_article"))
    if not candidates: return None, "no_signal"
    candidates.sort(key=lambda x: x[0])
    return candidates


# --------------------------------------------------------------------- toc

def strategy_toc(ctx: ItemContext, req: LookupRequest) -> Optional[LookupResult]:
    toc = ctx.toc
    if not toc: return None
    entries = toc.get("entries") or []
    if not entries: return None
    pages = parse_pages(req.pages)
    tt = _title_toks(req.title)
    best = None; best_score = 0
    for e in entries:
        et = e.get("title","")
        et_toks = _title_toks(et)
        # title match
        if tt and et_toks:
            score = 100 * len(tt & et_toks) / max(len(tt), len(et_toks))
        else: score = 0
        # printed page contains request start?
        if pages:
            pp = e.get("printed_pages")
            if pp and pp[0]:
                try:
                    a, b = int(pp[0][0]), int(pp[0][1])
                    if a <= pages[0] <= b: score += 30
                except Exception: pass
        if score > best_score:
            best = e; best_score = score
    if not best or best_score < 60: return None
    ranges = best.get("page_index_ranges") or []
    if not ranges or not ranges[0]: return None
    start_s, end_s = ranges[0]
    try:
        start_n = int(start_s.lstrip("n")); end_n = int(end_s.lstrip("n"))
    except Exception: return None
    # Override start/end leaves when the ILL request has printed pages we
    # can translate via scandata/pn.json/docling (ctx.printed_to_br). The
    # patron's citation is authoritative; published TOCs sometimes truncate
    # end-leaves to a section-header span, or even store start-leaf as the
    # TOC table page itself rather than the article body. Title-match is
    # only used to pick the right ENTRY; leaves come from the request when
    # available, fall back to TOC otherwise.
    pmap = {}
    try: pmap = ctx.printed_to_br
    except Exception: pass
    start_from_request = end_from_request = None
    if pages and pmap:
        if pages[0]:
            v = pmap.get(str(pages[0]))
            if v is not None: start_from_request = int(v)
        if len(pages) >= 2 and pages[1]:
            v = pmap.get(str(pages[1]))
            if v is not None: end_from_request = int(v)
    if start_from_request is not None:
        start_n = start_from_request
    if end_from_request is not None and end_from_request >= start_n:
        end_n = end_from_request
    # Ensure end >= start regardless of where the numbers came from
    if end_n < start_n:
        end_n = start_n
    # TOC entry quality — heuristic TOCs carry confidence and needs_qa flags.
    entry_conf = float(best.get("confidence") or 0.5)
    needs_qa = bool(best.get("needs_qa"))
    # Issue-level QA list: TOC carries `qa.entries_needing_qa`.
    qa_block = (toc.get("qa") or {})
    if best.get("id") in (qa_block.get("entries_needing_qa") or []):
        needs_qa = True
    # Title-only matches (no printed-page corroboration) are weaker
    title_only = (best_score < 90) and pages and best_score <= 100
    conf = min(100, 70 + int(best_score / 4))
    penalties = []
    if needs_qa:
        conf -= 25; penalties.append("needs_qa")
    if entry_conf < 0.6:
        conf -= 15; penalties.append(f"entry_conf={entry_conf}")
    if title_only and best_score < 80:
        conf -= 10; penalties.append("title_only_weak")
    conf = max(0, conf)
    return LookupResult(
        picked_item=ctx.item, start=start_n, end=end_n,
        strategy="toc", confidence=conf,
        evidence=[f"toc_match(score={best_score:.0f})",
                  f"entry_conf={entry_conf}",
                  f"penalties={penalties}" if penalties else "no_penalties"],
    )


# --------------------------------------------------------------------- docling

def strategy_docling(ctx: ItemContext, req: LookupRequest) -> Optional[LookupResult]:
    dl = ctx.docling
    if not dl: return None
    tt = _title_toks(req.title)
    if len(tt) < 3: return None
    # Build a list of (page_no, label, text, bbox_top) for title/section_header blocks
    headers = []
    for t in dl.get("texts") or []:
        if t.get("label") not in ("title", "section_header"): continue
        pr = t.get("prov") or []
        if not pr: continue
        pn = pr[0].get("page_no")
        bb = pr[0].get("bbox") or {}
        if pn is None: continue
        txt = (t.get("text") or "").strip()
        if not txt: continue
        headers.append((pn, t.get("label"), txt, bb.get("t") or 0))
    headers.sort()  # by page_no, then top y
    if not headers: return None
    # Find headers whose token-overlap with the requested title is high
    best = None; best_score = 0.0; best_idx = -1
    for i, (pn, lab, txt, _t) in enumerate(headers):
        h_toks = _title_toks(txt)
        if not h_toks: continue
        overlap = len(tt & h_toks)
        union = max(len(tt), len(h_toks))
        score = overlap / union
        # boost if the header's tokens are a clean superset of the request
        if tt.issubset(h_toks): score = max(score, 0.95)
        if score > best_score:
            best_score = score; best = (pn, lab, txt); best_idx = i
    if not best or best_score < 0.6: return None
    start_doc_page, lab, header_text = best
    pages = parse_pages(req.pages)
    end_supplied = bool(pages and pages[1] != pages[0])
    end_doc_page = None
    end_method = ""
    if end_supplied:
        # Patron supplied a printed end page → end = start + span (in pages).
        span = pages[1] - pages[0]
        end_doc_page = start_doc_page + span
        end_method = f"printed_span({span})"
    else:
        # Walk forward looking for the NEXT article (not just any section_header).
        # An article-boundary header has low token-overlap with the current article's
        # title; sub-section headers (Methods, Results, Discussion) overlap heavily
        # with the article context if they're terse and generic, but more reliably
        # we say: a header on a different docling page with text whose tokens are
        # mostly NOT in the current title is a candidate article boundary.
        current_tt = _title_toks(header_text) | tt
        for j in range(best_idx + 1, len(headers)):
            pn2, lab2, txt2, _t2 = headers[j]
            if pn2 == start_doc_page: continue
            if len(txt2) < 4: continue
            cand_toks = _title_toks(txt2)
            # Must look like an article title (multi-word, ≥3 meaningful tokens)
            if len(cand_toks) < 3: continue
            # And not be a sub-section of the current article
            if cand_toks & current_tt: continue
            end_doc_page = pn2 - 1
            end_method = f"next_article_header(pg={pn2})"
            break
        if end_doc_page is None:
            end_doc_page = ctx.n_leaves or (start_doc_page + 20)
            end_method = "end_of_issue"
    # Convert docling page_no → BR page-index. scandata leaf = docling page_no - 1.
    start_leaf_sd = start_doc_page - 1
    br_start = ctx.pi.scandata_to_br(start_leaf_sd)
    if br_start is None: br_start = start_leaf_sd
    end_leaf_sd = max(start_leaf_sd, end_doc_page - 1)
    br_end = ctx.pi.scandata_to_br(end_leaf_sd)
    if br_end is None: br_end = end_leaf_sd
    # Start confidence from title overlap (50–90)
    conf = 50 + int(best_score * 40)
    notes = []
    if end_supplied:
        conf += 5
        notes.append("end_supplied")
    elif end_method == "next_article_header":
        # Solid signal — next article boundary found
        conf += 5
        notes.append("next_article_found")
    elif end_method == "end_of_issue":
        # No next-article boundary found — much less reliable. Cap so this
        # case falls into the review band, not auto-deliver.
        conf = min(conf, 70)
        notes.append("end_of_issue_cap")
    # Bonus when page-map independently confirms the start
    pm_agrees = False
    if pages:
        pmap = ctx.printed_to_br
        pm_start = pmap.get(str(pages[0]))
        if pm_start is not None and abs(pm_start - br_start) <= 1:
            pm_agrees = True
            conf = min(100, conf + 10)
            notes.append("page_map_agrees")
        elif pm_start is not None:
            # Page-map says a different leaf — drop confidence
            conf = max(0, conf - 15)
            notes.append(f"page_map_disagrees(pm={pm_start})")
    return LookupResult(
        picked_item=ctx.item, start=br_start, end=br_end,
        strategy="docling", confidence=min(100, max(0, conf)),
        evidence=[f"docling_header_match({best_score:.0%})",
                   f"end_method={end_method}",
                   f"notes={notes}"],
    )


# --------------------------------------------------------------------- heuristic

def strategy_heuristic(ctx: ItemContext, req: LookupRequest) -> Optional[LookupResult]:
    pages = parse_pages(req.pages)
    if not pages: return None
    p_start = str(pages[0]); p_end = str(pages[1])
    pmap = ctx.printed_to_br
    br_s = pmap.get(p_start); br_e = pmap.get(p_end)
    end_supplied = (pages[1] != pages[0])
    if br_s is None:
        return None
    start_leaf_sd = ctx.pi.br_to_scandata(br_s) or br_s
    title_ok, frac, why = title_at_top_of_leaf(ctx, start_leaf_sd, req.title)
    end_method = None
    if br_e is None:
        br_e = br_s + (pages[1] - pages[0])
        end_method = "span_fallback"
    elif not end_supplied:
        scan = find_end_by_hocr_scan(ctx, start_leaf_sd, req.title)
        if isinstance(scan, list) and scan:
            br_e = ctx.pi.scandata_to_br(scan[0][0]) or scan[0][0]
            end_method = f"forward_scan_{scan[0][1]}"
        else:
            end_method = "no_end_signal"
    else:
        end_method = "page_map"
    end_leaf_sd = ctx.pi.br_to_scandata(br_e) or br_e
    end_ok = bool(REF_RE.search(ctx.leaf_text(end_leaf_sd)) or
                   REF_RE.search(ctx.leaf_text(end_leaf_sd + 1)))
    score = 30
    if end_supplied and pmap.get(p_end) is not None:
        expected = pages[1] - pages[0]; actual = br_e - br_s
        if abs(expected - actual) <= 2: score += 15
        score += 15
    elif not end_supplied and end_method and end_method.startswith("forward_scan"):
        score += 10
    if title_ok: score += 25
    if end_ok: score += 15
    return LookupResult(
        picked_item=ctx.item, start=br_s, end=br_e,
        strategy="heuristic", confidence=min(100, score),
        evidence=[why, end_method, f"end_ok={end_ok}"],
    )


# --------------------------------------------------------------------- driver

STRATEGIES = (strategy_toc, strategy_docling, strategy_heuristic)


def lookup(req: LookupRequest, cache_dir=DEFAULT_CACHE) -> LookupResult:
    tiers = search_candidates(req)
    audit = []
    best = LookupResult()
    for tier_name in ("T1", "T2", "T3"):
        for cand in tiers.get(tier_name, [])[:4]:
            ident = cand["identifier"]
            try: ctx = ItemContext(ident, cache_dir)
            except Exception as e:
                audit.append({"item": ident, "err": str(e)})
                continue
            for strat in STRATEGIES:
                try: r = strat(ctx, req)
                except Exception:
                    continue
                if r is None: continue
                # Tier penalty
                tier_pen = {"T1": 0, "T2": -5, "T3": -15}[tier_name]
                r.confidence = max(0, min(100, r.confidence + tier_pen))
                audit.append({"item": ident, "tier": tier_name,
                               "strat": r.strategy, "conf": r.confidence})
                if r.confidence > best.confidence:
                    best = r
                if r.confidence >= 90: break
            if best.confidence >= 90: break
        if best.confidence >= 90: break
    best.evidence = (best.evidence or []) + [{"audit": audit}]
    if not best.picked_item:
        best.error = "no_candidate_yielded_a_result"
    return best


# --------------------------------------------------------------------- cli

def _main():
    """Demo CLI: python3 tools/ill_lookup.py <issn> <vol> <iss> <yr> <pages> [title] [author]"""
    import argparse
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("issn"); ap.add_argument("vol", nargs="?", default="")
    ap.add_argument("iss", nargs="?", default=""); ap.add_argument("yr", nargs="?", default="")
    ap.add_argument("pages", nargs="?", default="")
    ap.add_argument("--title", default=""); ap.add_argument("--author", default="")
    args = ap.parse_args()
    req = LookupRequest(issn=args.issn, vol=args.vol, iss=args.iss, yr=args.yr,
                         pages=args.pages, title=args.title, author=args.author)
    r = lookup(req)
    print(json.dumps({
        "picked_item": r.picked_item, "start": r.start, "end": r.end,
        "strategy": r.strategy, "confidence": r.confidence,
        "evidence": [e for e in r.evidence if isinstance(e, str)],
        "error": r.error,
    }, indent=2))


if __name__ == "__main__":
    _main()
