#!/usr/bin/env python3
"""Regression tests for the genre answer engine. Deterministic only (no Ollama):
covers the three live failures. Run: python tests/test_essay.py"""
import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import essay

Q1 = ("In three to four sentences describe the most complex endpoint management or device "
      "automation project you personally owned. What was your role, what tools or systems did "
      "you use, and what changed as a result?")
Q2 = ("If you were given the opportunity to spend a month building a software application with "
      "an AI coding agent for use at home or at work, what would you build, and why? (100-250 words)")
Q3 = ("Tell us about a time you worked with Salesforce or another brittle business-automation "
      "platform and had to balance stakeholder tradeoffs.")
QW = "Describe a time you rebuilt or fixed a broken monitoring or SIEM system that you owned."

fails = []
def check(name, cond, extra=""):
    print(("PASS " if cond else "FAIL ") + name + ("" if cond else "  <-- " + str(extra)))
    if not cond: fails.append(name)

# --- constraints ---
c1 = essay.parse_constraints(Q1)
check("Q1 sentence range 3-4", c1["min_sent"] == 3 and c1["max_sent"] == 4, c1)
check("Q1 owned flag", c1["owned"] is True, c1)
c2 = essay.parse_constraints(Q2)
check("Q2 word range 100-250", c2["min_words"] == 100 and c2["max_words"] == 250, c2)

# --- genre (all three hit deterministic rules; no model) ---
check("Q1 genre owned_project", essay.classify_genre(Q1, c1) == "owned_project", essay.classify_genre(Q1, c1))
check("Q2 genre hypothetical (beats definition)", essay.classify_genre(Q2, c2) == "hypothetical", essay.classify_genre(Q2, c2))
c3 = essay.parse_constraints(Q3)
check("Q3 genre owned_project", essay.classify_genre(Q3, c3) == "owned_project", essay.classify_genre(Q3, c3))

# --- gap detection: Salesforce is a gap; endpoint management is a domain gap ---
g3 = essay.detect_gaps(Q3)
check("Q3 flags Salesforce as gap", any("salesforce" in x.lower() for x in g3["hard"]), g3)
g1 = essay.detect_gaps(Q1)
st1, sc1 = essay.retrieve_story(Q1, True)
dg = essay._domain_gap(Q1, st1)
check("Q1 endpoint management is a domain gap", dg is not None, (dg, (st1 or {}).get("id"), sc1))

# --- retrieval: a SIEM/monitoring question pulls a monitoring story (by domain, not a private id) ---
stw, scw = essay.retrieve_story(QW, True)
_doms = set((stw or {}).get("domains", []))
check("SIEM question retrieves a monitoring/siem story",
      bool(_doms & {"siem", "security monitoring", "observability", "monitoring"}),
      ((stw or {}).get("id"), _doms))

# --- validator: catches the exact old failures ---
bad1 = "I have experience with GitHub Actions and Ansible for deployment and operational automation."
check("validator rejects skills-dump opening", "banned_opening" in essay.validate(bad1, "owned_project", c1, {"hard":[],"limited":[]}), essay.validate(bad1,"owned_project",c1,{"hard":[],"limited":[]}))
bad2 = "To me, DevOps means bringing development and operations together with automation."
v2 = essay.validate(bad2, "hypothetical", c2, {"hard":[],"limited":[]})
check("validator rejects definition-as-hypothetical", "is_a_definition" in v2 or "not_a_proposal" in v2, v2)
check("validator rejects under-min-words hypothetical", "under_min_words" in " ".join(essay.validate(bad2,"hypothetical",c2,{"hard":[],"limited":[]})), essay.validate(bad2,"hypothetical",c2,{"hard":[],"limited":[]}))
bad3 = "I built a large Salesforce automation that streamlined the whole business."
check("validator rejects gap not disclosed", "gap_not_disclosed" in essay.validate(bad3, "owned_project", c3, g3), essay.validate(bad3,"owned_project",c3,g3))
good1 = "I rebuilt a dead Wazuh SIEM on Rocky Linux 9.7. I owned it end to end, fixed the indexer certs and agents, and captured a hardened image. Monitoring came back across the fleet."
check("validator passes a good owned_project answer", essay.validate(good1, "owned_project", c1, {"hard":[],"limited":[]}) == [], essay.validate(good1,"owned_project",c1,{"hard":[],"limited":[]}))

# --- gap_analog path is deterministic and honest (no model) ---
honest = essay._honest_gap(Q3, g3, None, c3)
check("gap answer opens with an honest denial", "have not worked" in honest.lower() or "not owned" in honest.lower(), honest[:80])

print("\n%d/%d checks passed" % (12 - len(fails) + 0, 12) if False else "")
print(("ALL PASS" if not fails else ("FAILURES: " + ", ".join(fails))))
sys.exit(1 if fails else 0)
