"""Read/write the story bank (stories.yaml) for the answer engine. Stdlib + yaml only.
The dashboard Stories editor and any end user add experiences here without touching YAML
by hand - this is the bank the model draws real narratives from (not the resume)."""
import os, re, yaml, threading

HERE = os.path.dirname(os.path.abspath(__file__))
PATH = os.path.join(HERE, "stories.yaml")
_LOCK = threading.Lock()

_LIST = ("domains", "tools", "not_implied")

def _slug(s):
    return re.sub(r"[^a-z0-9]+", "-", (s or "").lower()).strip("-")[:40] or "story"

def _as_list(v):
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    return [x.strip() for x in re.split(r"[,\n;]+", str(v or "")) if x.strip()]

def load():
    try:
        with open(PATH, encoding="utf-8") as f:
            d = yaml.safe_load(f) or {}
        return d.get("stories", []) or []
    except Exception:
        return []

class StoryExists(Exception):
    """create_only hit an id already in the bank. Nothing was written."""
    def __init__(self, sid):
        super().__init__(sid)
        self.sid = sid

def _commit(stories):
    """Caller holds _LOCK. Round-trip the new document, copy .bak, then replace."""
    tmp = PATH + ".tmp"
    header = (
        "# Penates story bank. The essay engine selects and dresses ONE of these\n"
        "# (or states a gap). Add real experiences here - resume bullets are not stories.\n"
    )
    body = yaml.safe_dump({"stories": stories}, sort_keys=False, allow_unicode=True, width=100)
    with open(tmp, "w", encoding="utf-8") as f:
        f.write(header)
        f.write(body)
    try:
        with open(tmp, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
        if not isinstance(doc, dict) or not isinstance(doc.get("stories"), list):
            raise ValueError("stories.yaml round-trip failed")
    except Exception:
        try:
            os.remove(tmp)
        except OSError:
            pass
        raise
    if os.path.exists(PATH):
        with open(PATH, "rb") as src, open(PATH + ".bak", "wb") as dst:
            dst.write(src.read())
    os.replace(tmp, PATH)

def save(stories):
    with _LOCK:
        _commit(stories)

def upsert(payload):
    """Add or update one story from a flat dict (form fields). Returns the stored story.

    create_only=true raises StoryExists when the id is already stored (no write).
    A non-empty source is kept on the story. Editor saves omit both and replace on id.
    Read, modify, and write share one lock.
    """
    payload = payload or {}
    sid = (payload.get("id") or "").strip() or _slug(payload.get("title"))
    story = {
        "id": sid,
        "title": (payload.get("title") or "").strip(),
        "domains": _as_list(payload.get("domains")),
        "tools": _as_list(payload.get("tools")),
        "not_implied": _as_list(payload.get("not_implied")),
        "owned": bool(payload.get("owned")),
        "star": {
            "situation": (payload.get("situation") or "").strip(),
            "task": (payload.get("task") or "").strip(),
            "action": (payload.get("action") or "").strip(),
            "result": (payload.get("result") or "").strip(),
        },
        "hero": (payload.get("hero") or "").strip() or (payload.get("title") or "").strip(),
    }
    source = payload.get("source")
    if source is not None and str(source).strip():
        story["source"] = str(source).strip()[:40]
    create_only = bool(payload.get("create_only"))
    with _LOCK:
        stories = load()
        for i, s in enumerate(stories):
            if s.get("id") == sid:
                if create_only:
                    raise StoryExists(sid)
                stories[i] = story
                break
        else:
            stories.append(story)
        _commit(stories)
    return story

def delete(sid):
    with _LOCK:
        stories = [s for s in load() if s.get("id") != sid]
        _commit(stories)
    return len(stories)
