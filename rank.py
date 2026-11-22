#!/usr/bin/env python3
"""Rank discovered jobs by PROFILE-RELATIVE fit (rule-based, no LLM).

Scores each row 0-100 against the user's actual profile.yaml, not by absolute
keyword counts. Title band and level ladder come from config.json "search"
(target_titles / related_titles / push_down_titles / level). HAVE/GAP come
from profile.yaml via skills.py.

  python rank.py            # score all 'discovered', show top 30
  python rank.py --top 50
  python rank.py --all      # score every row
  python rank.py --why URL  # explain one row's score
  python rank.py --smoke    # run the co-designed smoke checks (no save)
"""
import argparse, re
import jobs_store, skills

# --- constraint signals ------------------------------------------------------
# Location / remote / WV-exclusion HARD-CAPS were REMOVED: those are structured FILTERS
# now (serve.py params + dashboard checkboxes), not fit-score factors.
# Clearance + degree remain as in-score caps until filter toggles cover them.
TSSCI = re.compile(r"\bts/sci\b|\bts\s*sci\b|top secret|\bpolygraph\b|\bpoly\b|"
                   r"\bsci\b clearance|full[- ]scope", re.I)
# Secret clearance with clearance context; must not hit "secrets management".
REQ_SECRET = re.compile(
    r"\bsecret\s+(?:security\s+)?clearance\b|"
    r"\bclearance\b[^.]{0,60}\bsecret\b|"
    r"\bsecret\b[^.]{0,60}\bclearance\b",
    re.I)
DEGREE = re.compile(r"bachelor|master(?:'s|s)?\s+degree|\bb\.?s\.?\b required|"
                    r"degree in|graduate degree|phd|\bbachelor", re.I)
DEGREE_EQUIV = re.compile(
    r"\bequivalent\b|\bin[- ]lieu\b|\bor\s+(?:related\s+)?experience\b", re.I)
REQ_CLEAR = TSSCI  # alias used by score_row TS/SCI cap
COMPETITOR_CLOUDS = {"GCP"}

# User level -> rung index. Title rungs: entry=0, senior=2, staff=3, management=4.
# mid (1) has no title tokens; bare titles (no level word) always x1.0.
_USER_LEVEL_IDX = {
    "entry": 0,
    "mid": 1,
    "senior": 2,
    "staff": 3,
    "executive": 4,
}

# Title level phrases per rung. Associate and II/III/IV intentionally omitted.
# Longest phrases first within each rung list. Highest matching rung wins.
_TITLE_LEVEL_RUNGS = [
    ("management", 4, [
        "vice president", "director", "manager", "chief", "head", "vp",
    ]),
    ("staff", 3, [
        "distinguished", "principal", "architect", "staff", "lead",
    ]),
    ("senior", 2, [
        "senior", "snr", "sr",
    ]),
    ("entry", 0, [
        "entry-level", "intern", "junior", "jr",
    ]),
]

# "Data" must not fire on "Data Center" (infra compound, not a data-role title).
_PHRASE_SKIP_FOLLOW = {
    "data": frozenset({"center"}),
}


def _fmt(s, n=4):
    s = sorted(s)
    return (",".join(s[:n]) + ("+%d" % (len(s) - n) if len(s) > n else "")) if s else "-"


def _wb_re(phrase):
    """Word-boundary regex for a (possibly multi-word) phrase."""
    parts = re.split(r"\s+", phrase.strip())
    body = r"\s+".join(re.escape(p) for p in parts if p)
    if not body:
        return None
    return re.compile(r"(?<![A-Za-z0-9])" + body + r"(?![A-Za-z0-9])", re.I)


def _phrase_hit(text, phrase):
    """True when phrase hits text on word boundaries.

    Longest-first callers sort phrases themselves. Short ambiguous tokens listed
    in _PHRASE_SKIP_FOLLOW skip when the next token is a known compound
    continuation (Data + Center).
    """
    if not text or not phrase or not str(phrase).strip():
        return False
    phrase = str(phrase).strip()
    rx = _wb_re(phrase)
    if not rx:
        return False
    skip_next = _PHRASE_SKIP_FOLLOW.get(phrase.lower())
    for m in rx.finditer(text):
        if skip_next:
            nxt = re.match(r"\s+([A-Za-z0-9]+)", text[m.end():])
            if nxt and nxt.group(1).lower() in skip_next:
                continue
        return True
    return False


def _best_phrase(text, phrases):
    """Longest matching phrase, or None."""
    ordered = sorted(
        (str(p).strip() for p in (phrases or []) if p and str(p).strip()),
        key=len, reverse=True)
    for p in ordered:
        if _phrase_hit(text, p):
            return p
    return None


def _norm_search(search):
    search = search or {}
    level = str(search.get("level") or "mid").strip().lower()
    if level not in _USER_LEVEL_IDX:
        level = "mid"
    return {
        "target_titles": list(search.get("target_titles") or []),
        "related_titles": list(search.get("related_titles") or []),
        "push_down_titles": list(search.get("push_down_titles") or []),
        "exclude_titles": list(search.get("exclude_titles") or []),
        "level": level,
    }


def _title_band(role, search):
    """Return (tmult, tag, hit_phrase). Empty target_titles -> neutral x0.75."""
    targets = search["target_titles"]
    related = search["related_titles"]
    if not targets:
        return 0.75, "neutral", None
    th = _best_phrase(role, targets)
    rh = _best_phrase(role, related)
    if th and rh:
        if len(rh) > len(th):
            return 0.9, "NEAR", rh
        return 1.0, "CORE", th
    if th:
        return 1.0, "CORE", th
    if rh:
        return 0.9, "NEAR", rh
    return 0.75, "neutral", None


def _title_level_rung(role):
    """Highest matching title rung (name, idx), or None if unmapped / no level word."""
    best = None  # (idx, name)
    for name, idx, phrases in _TITLE_LEVEL_RUNGS:
        if _best_phrase(role, phrases):
            if best is None or idx > best[0]:
                best = (idx, name)
    if best is None:
        return None
    return best[1], best[0]


def _level_mult(user_level, role, apply_ladder):
    """Return (mult, rung_name_or_None, direction_or_None).

    direction is above, below, or match. match is only x1.00 (same rung).
    No level word or ladder off -> x1.0 and no direction, so no level token.
    """
    if not apply_ladder:
        return 1.0, None, None
    hit = _title_level_rung(role)
    if hit is None:
        return 1.0, None, None
    rung_name, title_idx = hit
    user_idx = _USER_LEVEL_IDX.get(user_level, 1)
    delta = title_idx - user_idx  # positive = title above user
    if delta == 0:
        mult, way = 1.0, "match"
    elif delta == 1:
        mult, way = 0.7, "above"
    elif delta >= 2:
        mult, way = 0.35, "above"
    elif delta == -1:
        mult, way = 0.8, "below"
    else:
        mult, way = 0.6, "below"
    return mult, rung_name, way


def _specialty_mult(role, search):
    """push_down_titles -> x0.6 on hit. Returns (mult, phrase_or_None)."""
    hit = _best_phrase(role, search["push_down_titles"])
    if hit:
        return 0.6, hit
    return 1.0, None


def _has_bachelor_or_master(degree):
    return bool(re.search(
        r"bachelor|master|\bb\.?s\.?\b|\bm\.?s\.?\b|\bmba\b|\bphd\b|\bdoctorate\b",
        degree or "", re.I))


def _holds_tssci(clearance_level):
    """True when the profile clearance names TS/SCI or Top Secret.

    A holder is not capped by a TS/SCI requirement. Secret-only stays capped.
    """
    return bool(re.search(
        r"ts\s*/?\s*sci|top\s+secret", clearance_level or "", re.I))


def _profile_rank_ctx():
    p = skills._profile()
    clearance = ((p.get("clearance") or {}).get("level") or "").strip()
    degree = ((p.get("education") or {}).get("degree") or "").strip()
    clouds = set(p.get("cloud") or [])
    return clearance, degree, clouds


def score_row(r, HAVE, GAP, SUPP, search=None, clearance_level=None,
              degree=None, my_clouds=None, profile=None, phrases=None):
    """Phase-A search-config scorer. Returns (score|None, reason).

    Title band from config search: target CORE x1.0, related NEAR x0.9, else
    neutral x0.75. Level ladder + push_down are separate multipliers. Empty
    target_titles -> neutral 0.75, level x1.0, and titles[none-set].
    Level token is level[<rung> x<mult> above|below|match].
    Thin JD: 85 * eff * tmult * level * specialty * min(len(preferred),4)/4.
    No usable desc -> None ('unscored').

    HAVE is the caller's set. Phrase scan uses `phrases` when passed, else
    HAVE-strings from `profile`. Neither reads profile.yaml. Omit both and
    the HAVE tokens themselves are the phrases.
    """
    if search is None:
        search = jobs_store.config_search()
    search = _norm_search(search)

    if clearance_level is None or degree is None or my_clouds is None:
        p_clearance, p_degree, p_clouds = _profile_rank_ctx()
        if clearance_level is None:
            clearance_level = p_clearance
        if degree is None:
            degree = p_degree
        if my_clouds is None:
            my_clouds = p_clouds
    my_clouds = set(my_clouds or [])

    role = (r.get("role") or "")
    desc = r.get("desc") or ""

    tmult, ttag, _thit = _title_band(role, search)
    apply_ladder = bool(search["target_titles"])
    lmult, lrung, lway = _level_mult(search["level"], role, apply_ladder)
    smult, sphrase = _specialty_mult(role, search)

    reason = ["title=%s(x%.2f)" % (ttag, tmult)]
    if not search["target_titles"]:
        reason.append("titles[none-set]")
    if lrung is not None:
        reason.append("level[%s x%.2f %s]" % (lrung, lmult, lway))
    if sphrase is not None:
        reason.append("specialty[%s](x%.2f)" % (sphrase, smult))

    if phrases is not None:
        jd_phrases = phrases
    elif profile is not None:
        jd_phrases = skills.have_phrases(profile)
    else:
        jd_phrases = skills.have_phrases(phrases=HAVE)
    required, preferred, found = skills.extract_jd_skills(desc, phrases=jd_phrases)
    if not (required or preferred) or (len(skills.clean_desc(desc)) < 120 and len(required | preferred) < 3):
        reason.append("UNSCORED(no usable desc)")
        return None, " ".join(reason)

    unknown = skills.unknown_required(desc) if found else []
    rcov, rmatch, rsupp, rgap = skills.coverage(required, HAVE, SUPP)
    pcov, pmatch, psupp, pgap = skills.coverage(preferred, HAVE, SUPP)
    mult = tmult * lmult * smult

    if found and (required or unknown):
        known_units = skills.coverage_units(required)
        credit = rcov * known_units
        units = known_units + len(unknown)
        cov = credit / units if units else 0.0
        conf = min(max(known_units, len(unknown) and 1), 4) / 4.0
        score = 90 * cov * conf * mult + min(pcov * 10, 10)
        reason.append("req cov=%.0f%%%s have[%s]" % (
            cov * 100, "" if conf == 1.0 else " conf=%.2f" % conf,
            _fmt(rmatch | rsupp)))
        if unknown:
            reason.append("unknown[%s]" % _fmt(set(unknown)))
    else:
        unk_all = skills.unknown_required("Requirements:\n" + skills.clean_desc(desc))
        eff = pcov * len(preferred) / (len(preferred) + len(unk_all)) if preferred else 0.0
        score = 85 * eff * mult * min(len(preferred), 4) / 4.0
        reason.append("thin-jd cov=%.0f%% have[%s]" % (eff * 100, _fmt(pmatch | psupp)))
        if unk_all:
            reason.append("unknown[%s]" % _fmt(set(unk_all)))

    named_gaps = rgap & GAP
    if found and named_gaps:
        pen = min(len(named_gaps) * 6, 20)
        score -= pen
        reason.append("GAP[%s]-%d" % (_fmt(named_gaps), pen))

    jd_all = required | preferred
    # Empty cloud list -> no competitor penalty (nothing to compare against).
    if my_clouds and (COMPETITOR_CLOUDS & jd_all) and not (my_clouds & jd_all):
        score -= 15
        reason.append("competitor-cloud-only-15")

    if r.get("desc_truncated"):
        reason.append("JD-TRUNCATED")

    req_text = skills.required_block_text(desc) if found else ""
    if REQ_CLEAR.search(req_text):
        # TS/SCI or Top Secret on the profile: the holder is not capped.
        if not _holds_tssci(clearance_level):
            score = min(score, 20)
            reason.append("FLAG[TS/SCI-required]<=20")
    elif (not clearance_level) and REQ_SECRET.search(req_text):
        # Cap only when profile clearance is missing/empty. Active Secret passes.
        score = min(score, 20)
        reason.append("FLAG[Secret-required]<=20")

    if (req_text and DEGREE.search(req_text)
            and not DEGREE_EQUIV.search(desc or "")
            and not _has_bachelor_or_master(degree)):
        score = min(score, 25)
        reason.append("FLAG[degree-required]<=25")

    return int(round(max(0, min(100, score)))), " ".join(reason)


def _score_all(rows):
    profile = skills._profile()
    HAVE = skills.have_set(profile)
    GAP = skills.gap_set(profile)
    SUPP = skills.supported_set(profile)
    search = _norm_search(jobs_store.config_search())
    clearance, degree, clouds = _profile_rank_ctx()
    for r in rows:
        r["score"], r["score_reason"] = score_row(
            r, HAVE, GAP, SUPP, search=search,
            clearance_level=clearance, degree=degree, my_clouds=clouds,
            profile=profile)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--top", type=int, default=30)
    ap.add_argument("--all", action="store_true")
    ap.add_argument("--show-all", action="store_true")
    ap.add_argument("--why", help="explain the score for the row with this apply_url/url substring")
    ap.add_argument("--smoke", action="store_true", help="run co-designed smoke checks, no save")
    a = ap.parse_args()

    if a.smoke:
        return _smoke()

    d = jobs_store.load(); q = d["queue"]
    if a.why:
        profile = skills._profile()
        HAVE = skills.have_set(profile)
        GAP = skills.gap_set(profile)
        SUPP = skills.supported_set(profile)
        for r in q:
            if a.why in (r.get("apply_url") or "") or a.why in (r.get("url") or ""):
                sc, rs = score_row(r, HAVE, GAP, SUPP, profile=profile)
                print("%s @ %s\n  score=%d\n  %s" % (r.get("role"), r.get("company"), sc, rs))
                return
        print("no row matched", a.why); return

    rows = q if a.all else [r for r in q if r.get("status") == "discovered"]
    _score_all(rows)
    jobs_store.save(d)
    if not a.show_all:
        rows = [r for r in rows if jobs_store.applyable(r)]
    rows.sort(key=lambda r: (r.get("score") or 0), reverse=True)
    label = "all" if a.all else "discovered"
    print("Top %d of %d %s jobs:\n" % (min(a.top, len(rows)), len(rows), label))
    for r in rows[:a.top]:
        print("  %3d  %-18s %-38s  %s" % (r.get("score") or 0,
              (r.get("company") or "")[:18], (r.get("role") or "")[:38], r.get("ats") or ""))
        print("       %s" % (r.get("score_reason") or ""))
    print("\nScores saved. Triage the top ones; GET /jobs?status=discovered for all.")


def _smoke():
    profile = skills._profile()
    HAVE = skills.have_set(profile)
    GAP = skills.gap_set(profile)
    SUPP = skills.supported_set(profile)
    # Injected DevOps search so smoke does not depend on live config.json.
    search = _norm_search({
        "target_titles": [
            "site reliability", "SRE", "devops", "devsecops", "platform engineer",
            "infrastructure engineer", "cloud engineer", "cloud infrastructure",
            "systems engineer", "reliability engineer", "cloud architect",
            "solutions architect", "infrastructure architect",
        ],
        "related_titles": [
            "systems administrator", "systems admin", "systems analyst", "linux",
            "sysadmin", "network engineer", "automation engineer", "build engineer",
            "release engineer", "cloud operations", "it engineer",
        ],
        "push_down_titles": [],
        "level": "senior",
    })
    cases = [
      ("GCP-only (no AWS)", {"role": "Cloud Engineer", "desc":
        "Requirements:\n- 5+ years on Google Cloud Platform (GCP)\n- BigQuery, GKE, and Terraform on GCP\n- Go programming\nResponsibilities:\n- Build data pipelines"},
        lambda s: s <= 30),
      ("Strong AWS/DevOps fit", {"role": "Senior DevOps Engineer", "desc":
        "What we're looking for:\n- Deep AWS experience (EC2, VPC, IAM, S3)\n- Kubernetes and Docker in production\n- Terraform and Ansible\n- CI/CD with GitHub Actions\n- Linux administration and Python"},
        lambda s: s >= 75),
      ("Title-weak but AWS-rich", {"role": "Engineer II", "desc":
        "Required:\n- AWS GovCloud, Terraform, Kubernetes, Docker\n- NIST compliance, Wazuh, Nessus\n- Bash and Python scripting"},
        lambda s: s >= 60),
      ("Onsite text now score-neutral (filtered in B, not capped)", {"role": "Platform Engineer", "location": "New York, NY (onsite required)", "desc":
        "Required:\n- AWS, Kubernetes, Terraform\nThis role is on-site in our NYC office, no remote."},
        lambda s: s >= 60),
      ("Thin aggregator snippet", {"role": "Senior SRE", "desc":
        "Requires 8+ years in SRE and infrastructure, container orchestration, cloud expertise and Bash."},
        lambda s: s is None or 20 <= s <= 70),
      ("Databricks/Spark required, AWS only in duties", {"role": "Senior Data Platform Engineer", "desc":
        "Requirements:\n- 5+ years with Databricks and Apache Spark\n- Python and SQL\n- Airflow orchestration\n"
        "Responsibilities:\n- Run workloads on AWS with Terraform and Kubernetes\n- Partner with analytics teams"},
        lambda s: s is not None and s < 40),
      ("TS/SCI in preferred does not cap", {"role": "Senior Systems Engineer", "desc":
        "Requirements:\n- AWS, Linux, Terraform, Ansible administration\n- Python and Bash scripting\n"
        "Preferred:\n- TS/SCI clearance\n- Master's degree"},
        lambda s: s is not None and s >= 60),
      ("Empty desc is unscored", {"role": "DevOps Engineer", "desc": ""}, lambda s: s is None),
    ]
    print("SMOKE (score_row):")
    ok = True
    for name, row, chk in cases:
        s, rs = score_row(row, HAVE, GAP, SUPP, search=search, profile=profile)
        p = chk(s)
        ok = ok and p
        print("  [%s] %-28s score=%4s  %s" % ("PASS" if p else "FAIL", name, s, rs))
    print("ALL PASS" if ok else "SOME FAILED")
    return 0 if ok else 1

if __name__ == "__main__":
    main()
