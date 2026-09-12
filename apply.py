#!/usr/bin/env python3
"""
apply.py — local job-application answer engine.

Merges: keyword-first classifier (tested) + length constraints and
company-interest variants (ChatGPT) + identity field matcher, confidence/pause
loop, cross-contamination guard, and strict variable contract (Grok).

Pipeline for a form field:
  1. Identity match  -> fields.yaml regex -> profile value (no LLM)
  2. Intent classify -> keyword score + confidence; Ollama tie-break only if murky
  3. Fill            -> variables from profile.yaml + application.yaml
                        (company_interest picks a variant; {{WHY_COMPANY}} via Ollama)
  4. Contract check  -> any unfilled {{VAR}} => PAUSE (never emit a half-answer)
  5. Length enforce  -> trim/compress to max_chars / max_sentences if set
  6. PAUSE fallback  -> low confidence => print for human, log to unmatched.log

Usage:
  python3 apply.py "Why do you want to work here?" --company "Stripe"
  python3 apply.py --field "Email address"                 # identity autofill
  python3 apply.py "Rate your Terraform experience" --raw --no-llm
  python3 apply.py "Tell us why you're a fit" --max-chars 300
  python3 apply.py --list
  python3 apply.py --unmatched                             # show the learning queue

Env: OLLAMA_MODEL (default llama3.2:3b), OLLAMA_HOST (default localhost:11434)
"""
import sys, os, re, json, argparse, urllib.request, hashlib
import yaml

HERE = os.path.dirname(os.path.abspath(__file__))

def load_env():
    """Minimal .env reader (no dependency). Real env vars win over the file."""
    p = os.path.join(HERE, ".env")
    if os.path.exists(p):
        for line in open(p):
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())
load_env()

MODEL = os.environ.get("OLLAMA_MODEL", "llama3.2:3b")
# Larger model for the grounded essay composer (falls back to MODEL until pulled).
ESSAY_MODEL = os.environ.get("OLLAMA_ESSAY_MODEL", MODEL)
# OpenAI-compatible chat-completions endpoint (matches the .env you set).
OLLAMA_BASE = os.environ.get("OLLAMA_BASE", "http://127.0.0.1:11434/v1/chat/completions")
OLLAMA_ROOT = OLLAMA_BASE.split("/v1", 1)[0]      # for the /api/tags health check
CONF_THRESHOLD = 0.35        # below this, and no keyword hit -> pause

def load(name): return yaml.safe_load(open(os.path.join(HERE, name)))

def dotted(obj, path):
    cur = obj
    for part in path.split("."):
        if not isinstance(cur, dict) or part not in cur:
            return None
        cur = cur[part]
    return cur

# ---- Ollama (OpenAI-compatible /v1/chat/completions) -----------------------
def ollama(prompt, system="", temperature=0.2, model=None):
    msgs = []
    if system: msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": model or MODEL, "messages": msgs, "temperature": temperature, "stream": False}
    req = urllib.request.Request(OLLAMA_BASE,
        data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.loads(r.read())["choices"][0]["message"]["content"].strip()

def ollama_up():
    try:
        urllib.request.urlopen(f"{OLLAMA_ROOT}/api/tags", timeout=3); return True
    except Exception:
        return False

# ---- 1. identity field matcher --------------------------------------------
def match_field(label, profile, fields, required=False):
    lab = label.lower()
    for rule in fields["fields"]:
        if re.search(rule["pattern"], lab, re.I):
            val = rule["value"]
            if val.startswith("literal:"): return val[8:], "field"
            if val.startswith("pause:"):   return None, "pause:" + val[6:]
            if val.startswith("ifreq:"):
                # fill from the profile ONLY when the form marks this field required;
                # otherwise leave blank (privacy: e.g. street address)
                if not required:
                    return None, "pause:optional"
                val = val[6:]
            resolved = dotted(profile, val)
            return (str(resolved) if resolved not in (None, "") else None), "field"
    return None, "no-field"

# ---- 2. classify with confidence ------------------------------------------
def score_intents(question, intents):
    q = question.lower()
    scored = []
    for intent, spec in intents.items():
        s = sum(2 + kw.count(" ") for kw in spec.get("keywords", []) if kw in q)
        if s: scored.append((s, intent))
    scored.sort(reverse=True)
    return scored

def classify(question, intents, use_llm=True):
    scored = score_intents(question, intents)
    if scored:
        top = scored[0][0]
        gap = top - (scored[1][0] if len(scored) > 1 else 0)
        conf = min(1.0, 0.5 + 0.1 * top + 0.1 * gap)   # heuristic
        if len(scored) == 1 or gap >= 2:
            return scored[0][1], conf, "keyword"
    else:
        conf = 0.0
    if not use_llm or not ollama_up():
        if scored and conf >= CONF_THRESHOLD:
            return scored[0][1], conf, "keyword-weak"
        return "pause", conf, "pause"
    shortlist = [i for _, i in scored[:6]] or list(intents.keys())
    sys_p = ("Map the job-application question to exactly one intent id from the list. "
             "Reply with ONLY the id. If none fit, reply: pause")
    pick = ollama(f"Question: {question}\n\nAllowed ids:\n" + "\n".join(shortlist) + "\npause",
                  sys_p, temperature=0).split()[0].strip().strip(".,")
    if pick == "pause" or pick not in intents:
        return ("pause", conf, "llm") if pick == "pause" else (shortlist[0], 0.5, "llm")
    return pick, 0.7, "llm"

# ---- 3. fill ---------------------------------------------------------------
STEM_RE = re.compile(
    r"^\s*(i['\u2019]?m\s+(interested\s+in|drawn\s+to)|i\s+am\s+(interested\s+in|drawn\s+to))\s+.*?\bbecause\s+",
    re.I)

def strip_stem(s):
    """Remove a leading 'I'm interested in/drawn to <X> because' the model may have added,
    so it doesn't double the template's own stem."""
    s = STEM_RE.sub("", s).strip()
    return s

def make_hook(app, use_llm, url=None, page_context=None):
    company = app.get("company", "the company")
    # Ground the company sentence in the REAL posting via the single generic path
    # (job_context.resolve_job_text -> extract_facts -> _clean_fact), NOT a hardcoded
    # application.yaml blurb. Same source every question uses; no company-specific rules.
    desc = ""
    try:
        import job_context as _jc
        _text, _src = _jc.resolve_job_text(url, page_context)
        if _text:
            desc = " ".join(_jc.extract_facts(_text)[:2]).strip()
    except Exception:
        pass
    if not use_llm or not ollama_up():
        return None
    if not desc:
        return None   # no verified company facts from the posting -> pause, never invent
    sys_p = (f"Complete this sentence with a reason clause: 'I'm interested in {company} because ...'. "
             f"Output ONLY the clause that comes AFTER the word 'because' — do not repeat the "
             f"company name, 'I'm interested', 'drawn to', or 'because'. Describe ONLY {company} — "
             "never mention any other company, product, or mission. Do not mention the candidate's "
             "skills. The clause MUST start with 'it', 'its', or 'they' so it reads correctly after 'because' (e.g. 'it builds ...', 'its platform helps ...'); never start with 'to'. One clause, lowercase start, end with a period.")
    s = ollama(f"Company: {company}\nAbout: {desc or company}\n\nReason clause:", sys_p, 0.4).strip().strip('"')
    s = strip_stem(s)
    if s:
        s = s[0].lower() + s[1:]
    # must read grammatically after "because": force a subject if the model returned
    # a purpose fragment ("to help..."/"help...") -> "it helps..." style.
    import re as _re
    if s and not _re.match(r"^(it|its|it's|they|their|the |of |because )", s, _re.I):
        s = "it " + s
    s = _re.sub(r"^it\s+to\s+", "it ", s, flags=_re.I)
    return s if s.endswith(".") else s + "."

def var_map(profile, app):
    d = dict(profile.get("defaults", {}))
    d.update(app.get("overrides", {}))
    d["COMPANY"] = app.get("company", "your company")
    d["ROLE"] = app.get("role_title", "this role")
    d["SALARY"] = str(app.get("salary", dotted(profile, "compensation.single_default") or ""))
    d["YEARS_IT"] = str(dotted(profile, "candidate.years_it") or "")
    return d

def fill(text, d):
    return re.sub(r"\{\{(\w+)\}\}", lambda m: str(d.get(m.group(1), m.group(0))), text)

# ---- 5. length enforcement -------------------------------------------------
def sentences(t): return re.findall(r'[^.!?]+[.!?]+', t.strip()) or [t.strip()]

def _no_emdash(t):
    """Owner preference + honesty note: generated answers never contain em/en dashes.
    Ranges between digits become a hyphen; elsewhere a dash becomes a comma."""
    if not t:
        return t
    t = re.sub(r"(\d)\s*[\u2013\u2014\u2015]\s*(\d)", r"\1-\2", t)   # 2020 - 2025 -> 2020-2025
    t = re.sub(r"\s*[\u2013\u2014\u2015]\s*", ", ", t)                    # spaced dash -> comma
    t = re.sub(r",\s*,", ",", t)
    t = re.sub(r"\s+([,.;:])", r"\1", t)
    t = re.sub(r"\s{2,}", " ", t)
    return t.strip()

def enforce_length(text, spec, max_chars, use_llm):
    max_s = spec.get("max_sentences")
    if max_s:
        s = sentences(text)
        if len(s) > max_s: text = " ".join(x.strip() for x in s[:max_s])
    limit = max_chars or spec.get("max_chars")
    if limit and len(text) > limit:
        if use_llm and ollama_up():
            text = ollama(text, f"Rewrite to under {limit} characters. Keep every fact identical, "
                                "add nothing, remove no claims. Output only the answer.", 0.2).strip()
        while len(text) > limit and len(sentences(text)) > 1:   # fallback hard trim
            text = " ".join(x.strip() for x in sentences(text)[:-1])
    return _no_emdash(text)

# ---- orchestration ---------------------------------------------------------
def log_unmatched(question):
    with open(os.path.join(HERE, "unmatched.log"), "a") as f:
        f.write(question.strip() + "\n")

def answer(question=None, field=None, cli_company=None, polish=True,
           use_llm=True, max_chars=None, app=None, required=False,
           url=None, page_context=None):
    profile = load("profile.yaml"); intents = load("answers.yaml")["intents"]
    if app is None:
        app = load("application.yaml")
    fields = load("fields.yaml")
    if cli_company: app["company"] = cli_company

    # identity field?
    if field:
        val, how = match_field(field, profile, fields, required=required)
        if how.startswith("pause"):
            return {"kind": how, "text": f"[PAUSE] leave '{field}' for the human ({how.split(':',1)[1]})."}
        if val is not None:
            return {"kind": "field", "intent": None, "text": val}
        return {"kind": "pause", "text": f"[PAUSE] no identity mapping for '{field}'."}

    intent, conf, how = classify(question, intents, use_llm=use_llm)
    if intent == "pause":
        log_unmatched(question)
        return {"kind": "pause", "confidence": round(conf, 2),
                "text": "[PAUSE] no confident intent. Logged to unmatched.log — "
                        "add a keyword/intent so this never pauses again."}

    spec = intents[intent]
    d = var_map(profile, app)

    if spec["type"] == "template" and "variants" in spec:
        style = app.get("interest_style") or d.get("INTEREST_STYLE") or spec["default_variant"]
        text = spec["variants"].get(style, spec["variants"][spec["default_variant"]])
    else:
        text = spec["answer"]

    if "{{WHY_COMPANY}}" in text:
        hook = make_hook(app, use_llm, url=url, page_context=page_context)
        if hook is None:
            return {"kind": "pause", "intent": intent,
                    "text": "[PAUSE] company_interest needs Ollama for the company sentence "
                            "(or fill {{WHY_COMPANY}} manually). Ollama not reachable."}
        text = text.replace("{{WHY_COMPANY}}", hook)

    text = fill(text, d)

    # strict variable contract: no leftover placeholders
    leftover = re.findall(r"\{\{(\w+)\}\}", text)
    if leftover:
        return {"kind": "pause", "intent": intent,
                "text": f"[PAUSE] missing variable(s): {', '.join(leftover)}. "
                        "Add to profile.yaml/application.yaml or answer manually."}

    if polish and spec["type"] == "template" and use_llm and ollama_up():
        text = ollama(text, "Lightly tighten this answer. Keep every fact identical, add nothing, "
                            "remove no claims. Output only the answer text.", 0.2).strip()

    # guard against a doubled stem: "...because I am drawn to <Co> because ..."
    if "variants" in spec:
        co = re.escape(d.get("COMPANY", ""))
        text = re.sub(
            rf"(because)\s+(i['\u2019]?m\s+(interested\s+in|drawn\s+to)|i\s+am\s+(interested\s+in|drawn\s+to))\s+{co}\s+because\s+",
            r"\1 ", text, flags=re.I)

    text = enforce_length(text, spec, max_chars, use_llm)
    return {"kind": "answer", "intent": intent, "confidence": round(conf, 2),
            "method": how, "chars": len(text), "text": text.strip()}


# ---- grounded essay composer (LOCAL AI does the complex answers) -----------
# The whole point of the project: recreate the paid "AI autofill" tier locally. For an
# open-ended question we ground the local model on the candidate's REAL material (work
# history + the closest prepared template as a fact bank) and have it compose an answer to
# the SPECIFIC question. Templates stop being the authority - they become source facts the
# model may reuse. Cached by question so runs are stable and fast on repeats.
CACHE_PATH = os.path.join(HERE, "data", "answer_cache.json")

def _cache_load():
    try: return json.load(open(CACHE_PATH))
    except Exception: return {}

def _cache_save(c):
    try:
        os.makedirs(os.path.dirname(CACHE_PATH), exist_ok=True)
        json.dump(c, open(CACHE_PATH, "w"), indent=0)
    except Exception: pass

def _condense_work_history(n=6):
    try:
        wh = load(os.path.join("data", "work_history.yaml")) or {}
        jobs = wh.get("jobs", []) if isinstance(wh, dict) else (wh or [])
    except Exception:
        jobs = []
    out = []
    for j in jobs[:n]:
        d = (j.get("description") or "").strip()
        out.append("- %s at %s (%s-%s): %s" % (
            j.get("title", ""), j.get("company", ""), j.get("start", ""),
            j.get("end", "") or "present", d))
    return "\n".join(out)

# ---- gap guard: don't let the model claim tech the candidate hasn't used ----
# A curated lexicon of technologies commonly named in these questions. For a question we
# find which of these it names, then check each against the candidate's REAL vocabulary
# (profile + work history). Anything the question centers on but the candidate lacks is a
# gap: we tell the model to be honest about it AND flag the field for human review.
TECH_LEXICON = [
    "kafka", "clickhouse", "rabbitmq", "redis", "memcached", "elasticsearch", "opensearch",
    "kubernetes", "k8s", "docker", "docker compose", "podman", "openshift", "rancher", "nomad",
    "terraform", "opentofu", "ansible", "puppet", "chef", "saltstack", "pulumi", "cloudformation", "cdk", "cdktf",
    "jenkins", "gitlab ci", "github actions", "circleci", "travis", "argocd", "flux", "helm", "spinnaker",
    "prometheus", "grafana", "nagios", "zabbix", "wazuh", "splunk", "datadog", "new relic", "elk", "graylog",
    "postgresql", "postgres", "mysql", "mariadb", "mongodb", "oracle", "sql server", "mssql",
    "dynamodb", "cassandra", "couchbase", "snowflake", "redshift", "bigquery", "databricks",
    "spark", "hadoop", "airflow", "dagster", "dbt", "flink", "beam", "nifi",
    "nginx", "haproxy", "apache", "envoy", "traefik", "istio", "linkerd", "consul", "vault",
    "aws", "azure", "gcp", "govcloud", "lambda", "ec2", "eks", "ecs", "fargate", "s3",
    "python", "golang", "java", "javascript", "typescript", "bash", "powershell", "ruby", "rust", "scala", "perl",
    "node.js", "react", "angular", "vue", "django", "flask", "spring", "dotnet", ".net",
    "vmware", "hyper-v", "proxmox", "openstack", "active directory", "sccm", "wsus", "foreman", "katello",
    "nessus", "qualys", "tenable", "sentinel", "crowdstrike", "okta", "ldap", "kerberos",
    "oracle rac", "rman", "goldengate", "kafka streams", "grpc", "graphql", "rest api",
]

def _tech_re(term):
    # word-ish boundaries; treat '.', '+', '#' literally; allow optional trailing 's'
    core = re.escape(term)
    return re.compile(r"(?<![a-z0-9])" + core + r"s?(?![a-z0-9])", re.I)

_CAND_TEXT = None
def _limited_map():
    """Technologies the candidate has SURFACE/working experience with -> honest qualifier.
    Not a full gap, but never upgraded to deep expertise."""
    try:
        m = load("profile.yaml").get("limited_experience", {}) or {}
    except Exception:
        m = {}
    return {str(k).lower(): str(v) for k, v in m.items()}

def _cand_text():
    global _CAND_TEXT
    if _CAND_TEXT is None:
        parts = []
        # ground truth ONLY - profile + work history. NOT answers.yaml (its templates assert
        # tools the candidate may not actually have, which would hide real gaps).
        for f in ("profile.yaml", os.path.join("data", "work_history.yaml")):
            try: parts.append(json.dumps(load(f)))
            except Exception: pass
        _CAND_TEXT = " ".join(parts).lower()
    return _CAND_TEXT

def _question_limited(question):
    """Limited-experience tech named in the question -> list of (display, qualifier)."""
    q = question or ""
    out = []
    for term, note in _limited_map().items():
        if _tech_re(term).search(q):
            out.append((_gap_disp(term), note))
    return out

def _question_gaps(question):
    """Tech named in the question that the candidate has no evidence of using."""
    q = question or ""
    cand = _cand_text()
    lim = set(_limited_map().keys())
    gaps = []
    for term in TECH_LEXICON:
        if term in lim:
            continue   # surface-level tech -> handled by the 'limited' tier
        rx = _tech_re(term)
        if rx.search(q) and not rx.search(cand):
            # normalise a couple of aliases so we don't double-flag
            gaps.append(term)
    # de-dup near-aliases (k8s/kubernetes, postgres/postgresql) - keep first seen
    seen = set(); out = []
    alias = {"k8s": "kubernetes", "postgres": "postgresql", "mssql": "sql server", "cdktf": "cdk"}
    for g in gaps:
        key = alias.get(g, g)
        if key not in seen:
            seen.add(key); out.append(g)
    return out

_GAP_DISPLAY = {
    "kafka": "Kafka", "clickhouse": "ClickHouse", "rabbitmq": "RabbitMQ", "redis": "Redis",
    "memcached": "Memcached", "elasticsearch": "Elasticsearch", "opensearch": "OpenSearch",
    "postgresql": "PostgreSQL", "postgres": "PostgreSQL", "mysql": "MySQL", "mongodb": "MongoDB",
    "cassandra": "Cassandra", "snowflake": "Snowflake", "redshift": "Redshift", "bigquery": "BigQuery",
    "databricks": "Databricks", "spark": "Spark", "hadoop": "Hadoop", "airflow": "Airflow",
    "flink": "Flink", "dbt": "dbt", "nifi": "NiFi", "docker compose": "Docker Compose",
    "gitlab ci": "GitLab CI", "github actions": "GitHub Actions", "sql server": "SQL Server",
    "golang": "Go", "javascript": "JavaScript", "typescript": "TypeScript", "graphql": "GraphQL",
    "istio": "Istio", "consul": "Consul", "vault": "Vault", "grpc": "gRPC",
}

def _gap_disp(t):
    return _GAP_DISPLAY.get(t, t[:1].upper() + t[1:])

def _human_list(items):
    xs = [_gap_disp(i) for i in items]
    if not xs: return ""
    if len(xs) == 1: return xs[0]
    if len(xs) == 2: return xs[0] + " and " + xs[1]
    return ", ".join(xs[:-1]) + ", and " + xs[-1]

def _real_stack(profile):
    parts = []
    for k in ("cloud", "containers", "iac", "cicd", "monitoring_security", "systems", "aws_services"):
        v = profile.get(k)
        if isinstance(v, list):
            parts += [str(x) for x in v]
    seen = []
    for x in parts:
        if x not in seen: seen.append(x)
    return ", ".join(seen[:12])

def _scrub_gaps(text, gaps):
    """Drop any sentence in `text` that names a gap tech - a curated template may itself
    assert a tool the candidate lacks, which would contradict the honest disclaimer."""
    if not text: return ""
    sents = re.split(r"(?<=[.!?])\s+", text)
    rxs = [_tech_re(g) for g in gaps]
    kept = [x for x in sents if x.strip() and not any(r.search(x) for r in rxs)]
    return " ".join(kept).strip()

def _honest_gap_answer(hard_gaps, limited_hits, tmpl, profile, limit):
    """Deterministic honest answer (NO model, so it can never overclaim) for a question that
    probes tech the candidate has no / only surface experience with. Discloses each honestly,
    then gives REAL experience (closest curated template with any over-claiming sentence
    scrubbed, else the profile stack). First person."""
    clauses = []
    if hard_gaps:
        clauses.append("I have not worked directly with " + _human_list(hard_gaps))
    for disp, note in limited_hits:
        clauses.append("my experience with %s is %s" % (disp, note))
    disclosure = "To be straightforward, " + "; ".join(clauses) + "."
    scrub_terms = list(hard_gaps) + [d.lower() for d, _ in limited_hits]
    body = _scrub_gaps((tmpl or "").strip(), scrub_terms)
    if len(body) >= 60:
        ans = disclosure + " My most relevant experience is this: " + body
    else:
        stack = _real_stack(profile)
        ans = disclosure + " My hands-on experience centers on " + stack + "."
    return enforce_length(ans, {}, limit, False).strip()

def compose(question, max_chars=None, app=None, model=None):
    """Grounded answer for an open-ended question. Returns the same dict shape as answer()."""
    profile = load("profile.yaml"); intents = load("answers.yaml")["intents"]
    if app is None: app = load("application.yaml")
    model = model or ESSAY_MODEL
    limit = max_chars or 900

    key = hashlib.sha1(("%s|%s|%s" % (model, limit, question.strip())).encode("utf-8")).hexdigest()
    cache = _cache_load()
    if key in cache and cache[key].strip():
        return {"kind": "answer", "intent": "compose", "method": "cache",
                "chars": len(cache[key]), "text": cache[key].strip()}

    # closest prepared template -> fact bank (not the final answer)
    intent, conf, how = classify(question, intents, use_llm=ollama_up())
    tmpl = ""
    if intent != "pause":
        spec = intents.get(intent, {})
        if spec.get("type") == "template" and "variants" in spec:
            tmpl = spec["variants"].get(spec.get("default_variant", ""), "")
        else:
            tmpl = spec.get("answer", "")
        try: tmpl = fill(tmpl, var_map(profile, app))
        except Exception: pass
        if "{{" in tmpl:   # unresolved placeholder -> don't feed a broken sentence
            tmpl = re.sub(r"\{\{[^}]+\}\}", "", tmpl)

    # GAP GUARD (deterministic, no model): if the question probes tech the candidate has no
    # evidence of using, do NOT let the model write it - it will overclaim. Build an honest
    # answer from real material and flag it for review. Works even if Ollama is down.
    hard_gaps = _question_gaps(question)
    limited_hits = _question_limited(question)
    if hard_gaps or limited_hits:
        honest = _honest_gap_answer(hard_gaps, limited_hits, tmpl, profile, limit)
        if honest:
            cache[key] = honest; _cache_save(cache)
            flag = list(hard_gaps) + [d for d, _ in limited_hits]
            return {"kind": "answer", "intent": intent or "compose", "method": "honest-template",
                    "chars": len(honest), "text": honest, "gaps": flag}

    if not ollama_up():
        if tmpl:
            return {"kind": "answer", "intent": intent, "method": "template-fallback",
                    "chars": len(tmpl), "text": enforce_length(tmpl, {}, limit, False).strip()}
        return {"kind": "pause", "text": "[PAUSE] Ollama offline and no template to fall back on."}

    ctx = "CANDIDATE WORK HISTORY (the only employers/dates/facts you may use):\n" + _condense_work_history()
    if tmpl:
        ctx += "\n\nPREPARED NOTES you may reuse or adapt (already true for this candidate):\n" + tmpl.strip()

    sys_p = ("You draft a job-application answer in FIRST PERSON as the candidate, using ONLY "
             "the facts in CONTEXT. Absolute rules:\n"
             "- NEVER state a number, metric, throughput, scale, team size, or dollar figure "
             "unless that exact figure appears in CONTEXT.\n"
             "- NEVER claim a specific employer, job title, date, certification, tool, or "
             "technology that is not in CONTEXT.\n"
             "- If the QUESTION names a technology or asks for an example you do NOT have in "
             "CONTEXT, DO NOT invent a project. Instead answer honestly: describe your closest "
             "real, related experience from CONTEXT and, if applicable, note you have not used "
             "that specific tool but can speak to equivalent work. Honesty beats a fabricated "
             "example.\n"
             "- Do not invent named incidents, root causes, or outcomes.\n"
             "Answer the SPECIFIC question, addressing each part it asks. Concrete, professional, "
             "first person, no filler, no preamble. Keep it under %d characters. Output ONLY the "
             "answer text." % limit)
    try:
        gen = ollama("CONTEXT:\n%s\n\nQUESTION:\n%s\n\nAnswer:" % (ctx, question),
                     sys_p, 0.2, model=model).strip().strip('"')
    except Exception as e:
        if tmpl:
            return {"kind": "answer", "intent": intent, "method": "template-fallback",
                    "chars": len(tmpl), "text": enforce_length(tmpl, {}, limit, False).strip()}
        return {"kind": "pause", "text": "[PAUSE] compose failed: %s" % e}

    gen = enforce_length(gen, {}, limit, True).strip()
    if not gen and tmpl:
        gen = enforce_length(tmpl, {}, limit, False).strip()
    if gen:
        cache[key] = gen; _cache_save(cache)
    return {"kind": "answer", "intent": intent or "compose",
            "method": "compose:" + model, "chars": len(gen), "text": gen, "gaps": []}

# ---- CLI -------------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("question", nargs="*")
    ap.add_argument("--field", help="identity field label to autofill")
    ap.add_argument("--company")
    ap.add_argument("--max-chars", type=int)
    ap.add_argument("--raw", action="store_true", help="no Ollama polish/hook")
    ap.add_argument("--no-llm", action="store_true", help="keyword-only classification")
    ap.add_argument("--list", action="store_true")
    ap.add_argument("--unmatched", action="store_true", help="show the learning queue")
    args = ap.parse_args()

    if args.list:
        for i, s in load("answers.yaml")["intents"].items():
            lim = f" [<= {s['max_sentences']}s]" if s.get("max_sentences") else ""
            var = " [variants]" if "variants" in s else ""
            print(f"{s['type'][:4]:5} {i:24}{var}{lim} {', '.join(s.get('keywords', [])[:3])}")
        return
    if args.unmatched:
        p = os.path.join(HERE, "unmatched.log")
        print(open(p).read() if os.path.exists(p) else "(empty)"); return

    if args.field:
        r = answer(field=args.field, cli_company=args.company); print(r["text"]); return

    q = " ".join(args.question).strip() or sys.stdin.read().strip()
    if not q: print("provide a question or --field", file=sys.stderr); sys.exit(1)

    r = answer(q, cli_company=args.company, polish=not args.raw,
               use_llm=not args.no_llm, max_chars=args.max_chars)
    meta = " ".join(f"{k}={v}" for k, v in r.items() if k != "text")
    print(f"# {meta}\n"); print(r["text"])

if __name__ == "__main__":
    main()
