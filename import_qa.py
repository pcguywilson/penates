#!/usr/bin/env python3
"""Import a Q&A export (from ChatGPT or any AI chat) into Penates as a review-flagged
reference. Cleans AI "wrapper" text, splits multi-option answers into variants, dedupes by
question, and writes data/imported_answers.json.

These are NOT trusted answers. The engine surfaces a match as a DRAFT you review and edit
before inserting; approving (insert) promotes it to your learned store. Nothing is
auto-submitted or auto-trusted, and the gap-guard still governs anything generated fresh.

  python import_qa.py my_export.json
  python import_qa.py my_export.json --llm     # extra cleaning pass on local Ollama (stragglers)
  python import_qa.py my_export.md
  python import_qa.py my_export.json --dry-run  # show stats, write nothing
"""
import os, re, json, sys, argparse, time

HERE = os.path.dirname(os.path.abspath(__file__))
DEST = os.path.join(HERE, "data", "imported_answers.json")

def _norm_q(q):
    return re.sub(r"[^a-z0-9]+", " ", (q or "").lower()).strip()

# ---- parsing: accept several shapes -----------------------------------------
def parse_file(path):
    """Return a list of {question, answer, category}. Accepts:
       - JSON {"items":[{question,answer,category?}]}  (Penates schema / the QA-extract shape)
       - JSON [{question,answer,...}]
       - JSON {"answers":{"<q>":{...}}}
       - Markdown:  ## <question>\n<answer...>  (also ### / ** **)"""
    raw = open(path, encoding="utf-8").read()
    ext = os.path.splitext(path)[1].lower()
    if ext in (".json",):
        d = json.loads(raw)
        items = None
        if isinstance(d, dict):
            items = d.get("items") or d.get("qa") or d.get("questions")
            if items is None and isinstance(d.get("answers"), dict):
                items = [dict(v, question=v.get("question") or k)
                         for k, v in d["answers"].items()]
        elif isinstance(d, list):
            items = d
        if items is None:
            sys.exit("JSON has no items/qa/answers list I recognize.")
        out = []
        for it in items:
            if not isinstance(it, dict):
                continue
            q = it.get("question") or it.get("q") or it.get("prompt") or ""
            a = it.get("answer") or it.get("a") or it.get("response") or ""
            out.append({"question": str(q).strip(), "answer": str(a),
                        "category": (it.get("category") or it.get("cat") or "").strip()})
        return out
    # markdown
    blocks = re.split(r"\n(?=#{2,3}\s+|\*\*Q(?:uestion)?\b)", raw)
    out = []
    for b in blocks:
        m = re.match(r"\s*(?:#{2,3}\s+|\*\*Q(?:uestion)?[:\*\s]+)(.+)", b)
        if not m:
            continue
        q = m.group(1).strip().strip("*").strip()
        a = b[m.end():].strip()
        a = re.sub(r"^\*\*A(?:nswer)?[:\*\s]+", "", a).strip()
        if q and a:
            out.append({"question": q, "answer": a, "category": ""})
    return out

# ---- cleaning: strip AI wrapper, split multi-option into variants ------------
_OPT_SPLIT = re.compile(r"\n?\s*(?:\*\*|#{2,4}\s*)?Option\s+[A-Z0-9][^\n*#]*(?:\*\*|)\s*\n", re.I)
_HDR_OPT   = re.compile(r"\n#{2,4}\s+[^\n]+\n")   # '### Neutral / Growth-Focused' style option headers
_LEAD_CHAT = re.compile(r"^(here\s*(?:'|’)?s|here are|that\s*(?:'|’)?s|below is|sure[,!]|great|"
                        r"i\s*(?:'|’)?d suggest|i would suggest|absolutely|of course|"
                        r"you can (?:use|choose|tailor)|pick the one)[^\n]*\n+", re.I)
_TRAIL_OFFER = re.compile(r"\n+(?:let me know|want me to|would you like|i can (?:also|help)|"
                          r"do you want|feel free)[^\n]*$", re.I)

def clean_answer(ans):
    if not ans:
        return []
    t = ans.strip()
    # split "**Option A ...**" menus
    parts = _OPT_SPLIT.split(t)
    if len(parts) > 1:
        parts = parts[1:]                     # drop the preamble before Option A
    else:
        # try "### Header" style option menus (keep bodies under each header)
        hdrs = _HDR_OPT.split(t)
        parts = hdrs if len(hdrs) > 1 else [t]
    out = []
    for v in parts:
        v = v.strip()
        m = re.search(r"\*\*Answer:?\*\*\s*", v)     # cut to explicit answer marker
        if m:
            v = v[m.end():]
        v = _LEAD_CHAT.sub("", v)
        v = _TRAIL_OFFER.sub("", v)
        v = re.sub(r"\n?-{3,}\n?", " ", v)           # markdown rules
        v = re.sub(r"^>\s?", "", v, flags=re.M)      # blockquotes
        v = re.sub(r"\*\*|__", "", v)                # bold
        v = re.sub(r"^#{1,6}\s+", "", v, flags=re.M) # stray headers
        v = re.sub(r"[ \t]{2,}", " ", v)
        v = re.sub(r"\n{3,}", "\n\n", v).strip()
        if len(v) >= 40:
            out.append(v)
    seen, uniq = set(), []
    for v in out:
        k = _norm_q(v)[:120]
        if k not in seen:
            seen.add(k); uniq.append(v)
    return uniq[:4]                                   # cap variants

def _needs_llm(variants, raw):
    """Heuristic couldn't fully clean: still smells like AI commentary."""
    if not variants:
        return True
    head = variants[0][:200].lower()
    return bool(re.search(r"option [a-z0-9]|here('|’)?s|pick the|you can (use|choose|tailor)|"
                          r"depending on|feel free", head))

def clean_llm(raw):
    """Optional local-Ollama extraction for stragglers. Never adds facts."""
    try:
        import apply as _a
        if not _a.ollama_up():
            return None
        sysp = ("Extract ONLY the usable job-application answer from this AI chat response. "
                "Remove all commentary, option labels, and 'here is' framing. If it offers "
                "multiple options, return the single best one. Do NOT add, change, or invent "
                "any fact. First person. Output only the answer text.")
        out = _a.ollama(raw[:4000], sysp, temperature=0).strip().strip('"')
        return out if len(out) >= 40 else None
    except Exception:
        return None

def load_store():
    try:
        return json.load(open(DEST, encoding="utf-8"))
    except Exception:
        return {"answers": {}}

def save_store(s):
    os.makedirs(os.path.dirname(DEST), exist_ok=True)
    tmp = DEST + ".tmp"
    json.dump(s, open(tmp, "w", encoding="utf-8"), indent=1, ensure_ascii=False)
    os.replace(tmp, DEST)

def run(path, use_llm=False, dry=False, source=None):
    items = parse_file(path)
    store = load_store()
    answers = store.setdefault("answers", {})
    added = updated = skipped = llm_used = 0
    for it in items:
        q = it["question"].strip()
        if not q:
            skipped += 1; continue
        variants = clean_answer(it["answer"])
        if use_llm and _needs_llm(variants, it["answer"]):
            v = clean_llm(it["answer"])
            if v:
                variants = [v] + [x for x in variants if x != v]; llm_used += 1
        if not variants:
            skipped += 1; continue
        k = _norm_q(q)
        rec = {"question": q[:400], "variants": variants,
               "category": it.get("category", ""), "source": source or os.path.basename(path),
               "imported_at": int(time.time()), "status": "reference"}
        if k in answers:
            answers[k] = rec; updated += 1
        else:
            answers[k] = rec; added += 1
    store["last_import"] = {"file": os.path.basename(path), "at": int(time.time()),
                            "added": added, "updated": updated, "skipped": skipped}
    if not dry:
        save_store(store)
    return {"parsed": len(items), "added": added, "updated": updated,
            "skipped": skipped, "llm_used": llm_used, "total": len(answers)}

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("file")
    ap.add_argument("--llm", action="store_true", help="local-Ollama cleaning pass for stragglers")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    r = run(args.file, use_llm=args.llm, dry=args.dry_run)
    print("parsed=%(parsed)d  added=%(added)d  updated=%(updated)d  skipped=%(skipped)d  "
          "llm_used=%(llm_used)d  store_total=%(total)d" % r)
    if args.dry_run:
        print("(dry run - nothing written)")

if __name__ == "__main__":
    main()
