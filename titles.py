#!/usr/bin/env python3
"""Discovery title gate. config.json "search" wins; companies.yml is fallback.

keep(title) is true when the title hits any target_titles or related_titles
phrase on a word boundary and hits no exclude_titles phrase. push_down_titles
are score-only (rank.py) and never drop a row here.

When target_titles and related_titles are both empty, positives/negatives come
from companies.yml title_filter instead. Config and the YAML filter are not
combined.
"""
import json, os, re

HERE = os.path.dirname(os.path.abspath(__file__))


def _wb_re(phrase):
    """Word-boundary regex for a (possibly multi-word) phrase.

    Same boundary as rank._wb_re: letters and digits stick to the token,
    hyphens do not. "Manager" does not match "Management".
    """
    parts = re.split(r"\s+", str(phrase).strip())
    body = r"\s+".join(re.escape(p) for p in parts if p)
    if not body:
        return None
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.I)


def phrase_hit(text, phrase):
    if not text or not phrase or not str(phrase).strip():
        return False
    rx = _wb_re(phrase)
    return bool(rx and rx.search(str(text)))


def _phrases(items):
    if isinstance(items, str):
        items = [items]
    if not isinstance(items, (list, tuple)):
        return []
    out = []
    for p in items:
        if p and str(p).strip():
            out.append(str(p).strip())
    return out


def _any(text, phrases):
    return any(phrase_hit(text, p) for p in phrases)


def _load_search():
    path = os.path.join(HERE, "config.json")
    try:
        with open(path, encoding="utf-8") as f:
            cfg = json.load(f)
    except Exception:
        return {}
    search = cfg.get("search") if isinstance(cfg, dict) else None
    return search if isinstance(search, dict) else {}


def load_title_filter():
    """companies.yml title_filter, else companies.example.yml, else {}."""
    try:
        import yaml
    except ImportError:
        return {}
    for name in ("companies.yml", "companies.example.yml"):
        path = os.path.join(HERE, "config", name)
        if not os.path.exists(path):
            continue
        try:
            with open(path, encoding="utf-8") as f:
                cfg = yaml.safe_load(f) or {}
        except Exception:
            continue
        tf = cfg.get("title_filter") if isinstance(cfg, dict) else None
        if isinstance(tf, dict):
            return tf
    return {}


def keep(title, search=None, title_filter=None):
    """True if this title stays in discovery / ATS scan.

    search: config.json search block. None reads config.json.
    title_filter: companies.yml title_filter, used only when target_titles and
    related_titles are both empty. None reads companies.yml in that case.
    A passed title_filter is ignored while config search has a target or related
    phrase (config wins; the two lists are not merged).
    """
    if search is None:
        search = _load_search()
    search = search or {}
    targets = _phrases(search.get("target_titles"))
    related = _phrases(search.get("related_titles"))
    if targets or related:
        if not _any(title, targets + related):
            return False
        if _any(title, _phrases(search.get("exclude_titles"))):
            return False
        return True
    if title_filter is None:
        title_filter = load_title_filter()
    title_filter = title_filter or {}
    pos = _phrases(title_filter.get("positive"))
    neg = _phrases(title_filter.get("negative"))
    if pos and not _any(title, pos):
        return False
    if _any(title, neg):
        return False
    return True
