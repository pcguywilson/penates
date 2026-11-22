"""Plain-English reason for a job's score, built from rank.py's score_reason tokens
and the same skill coverage rank.py uses. No model calls."""
import re

import skills


def _list(xs, n=6):
    xs = sorted(xs)
    return ", ".join(xs[:n]) + (" and %d more" % (len(xs) - n) if len(xs) > n else "")


_LEVEL_WORDS = {"entry": "an entry-level", "mid": "a mid-level", "senior": "a senior",
                "staff": "a staff/principal/lead", "management": "a management", "executive": "a management"}


def _title_sentence(why):
    if "titles[none-set]" in why:
        s = "You haven't set target titles yet (Settings > What you're looking for), so titles are not scored"
    elif "CORE" in why:
        s = "Title matches one of your target titles"
    elif "NEAR" in why:
        s = "Title matches one of your related titles"
    elif "off-target" in why:
        s = "Title is outside what you are looking for"
    else:
        s = "Title is not one of your target titles"
    m = re.search(r"level\[(\w+) x([\d.]+)(?: (above|below|match))?\]", why)
    if m:
        rung, mult, way = m.group(1).lower(), float(m.group(2)), m.group(3)
        if not way:
            way = "match" if mult >= 1.0 else ("below" if rung == "entry" else "above")
        word = _LEVEL_WORDS.get(rung, "a")
        if way == "match":
            s += "; it is %s role, which matches your level" % word
        else:
            s += "; it is %s role, %s your level, so it is scored down (x%.2g)" % (word, way, mult)
    return s + "."


def explain(r, HAVE=None, SUPP=None):
    """One short paragraph: title fit, level, skills you have / partly have / lack, caps."""
    why = r.get("score_reason") or ""
    if r.get("score") is None or "UNSCORED" in why:
        return "Not scored: the posting has too little detail. Open it and judge it yourself."
    HAVE = HAVE if HAVE is not None else skills.have_set()
    SUPP = SUPP if SUPP is not None else skills.supported_set()
    out = [_title_sentence(why)]
    m = re.search(r"specialty\[([^\]]+)\]", why)
    if m:
        out.append("It centers on %s, which you marked as a specialty you don't do, so it is scored down."
                   % m.group(1).replace(",", ", "))
    desc = r.get("desc") or ""
    req, pref, found = skills.extract_jd_skills(desc)
    use = req if (found and req) else (req | pref)
    if use:
        _, have, part, gap = skills.coverage(use, HAVE, SUPP)
        lead = ("Of the skills it requires" if (found and req)
                else "It has no separate requirements list; of the skills it mentions")
        bits = []
        if have: bits.append("you have %s" % _list(have))
        if part: bits.append("partly have %s" % _list(part))
        if gap: bits.append("don't have %s" % _list(gap))
        out.append("%s, %s." % (lead, "; ".join(bits) if bits else "you have none"))
        if len(use) < 4:
            out.append("It names only a few skills, so the score is a rough guess.")
    if "FLAG[TS/SCI" in why or "FLAG[Secret" in why:
        out.append("It requires a security clearance you don't have, so the score is capped at 20.")
    if "FLAG[degree" in why:
        out.append("It requires a degree with no 'or equivalent experience' option, so the score is capped at 25.")
    if "competitor-cloud" in why:
        out.append("It is built around a cloud platform you don't list.")
    if "JD-TRUNCATED" in why:
        out.append("The posting text was cut off, so some requirements may be missing.")
    return " ".join(out)
