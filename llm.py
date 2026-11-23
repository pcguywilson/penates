"""Chat router for answer and essay calls.

apply.ollama keeps its signature and delegates here. Ollama stays the default
when config.json has no "llm" block. claude_cli and grok_cli run the local
CLIs (subscription logins) with tools disabled. API keys are read from secrets.yaml
(anthropic_api_key / xai_api_key) or ANTHROPIC_API_KEY / XAI_API_KEY. They are
never logged and never placed in a response or exception message.
"""
import json
import os
import re
import shutil
import subprocess
import tempfile
import urllib.request

import yaml

HERE = os.path.dirname(os.path.abspath(__file__))

XAI_URL = "https://api.x.ai/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
MAX_TOKENS = 2048
TIMEOUT = 120

PROVIDERS = ("ollama", "anthropic", "xai", "claude_cli", "grok_cli")
CLI_PROVIDERS = ("claude_cli", "grok_cli")
CLI_TIMEOUT = 180
_DEFAULT_CLI = {"claude_bin": "claude", "grok_bin": "grok"}
_KEY_FIELDS = {"anthropic": "anthropic_api_key", "xai": "xai_api_key"}
_ENV_FIELDS = {"anthropic": "ANTHROPIC_API_KEY", "xai": "XAI_API_KEY"}
_UNCHANGED = object()
# CSI and single-char ESC sequences from CLI TUIs.
_ANSI_RE = re.compile(r"\x1b(?:[@-Z\\-_]|\[[0-?]*[ -/]*[@-~])")

# Filled by complete(). provider is who answered; fell_back is true only when
# a cloud error was retried on the local Ollama model and that retry returned text.
last_meta = {"provider": "ollama", "fell_back": False, "model": ""}


class SettingsError(Exception):
    """Safe for HTTP responses: the message never contains key material."""


def secrets_path():
    return os.path.join(HERE, "secrets.yaml")


def _nonempty(value):
    if isinstance(value, str) and value.strip():
        return value.strip()
    return ""


def _load_secrets():
    path = secrets_path()
    try:
        with open(path, encoding="utf-8") as f:
            doc = yaml.safe_load(f)
    except Exception:
        return {}
    return doc if isinstance(doc, dict) else {}


def _secret_values():
    """Every configured key, so a leaked token can be stripped from errors."""
    found = []
    doc = _load_secrets()
    for provider, field in _KEY_FIELDS.items():
        file_val = _nonempty(doc.get(field))
        if len(file_val) >= 8:
            found.append(file_val)
        env_val = _nonempty(os.environ.get(_ENV_FIELDS[provider]) or "")
        if len(env_val) >= 8:
            found.append(env_val)
    return sorted(set(found), key=len, reverse=True)


def scrub_text(text):
    out = str(text)
    for secret in _secret_values():
        if secret in out:
            out = out.replace(secret, "[redacted]")
    return out


def api_key(provider):
    """File wins over env. Empty string when unset. Do not log the result."""
    field = _KEY_FIELDS[provider]
    doc = _load_secrets()
    file_val = _nonempty(doc.get(field))
    if file_val:
        return file_val
    return _nonempty(os.environ.get(_ENV_FIELDS[provider]) or "")


def key_set(provider):
    return bool(api_key(provider))


def _resolve(cfg):
    import apply as _a
    cfg = cfg if isinstance(cfg, dict) else {}
    block = cfg.get("llm")
    if not isinstance(block, dict):
        return {
            "answers": {"provider": "ollama", "model": _nonempty(cfg.get("ollama_model")) or _a.MODEL},
            "essays": {"provider": "ollama", "model": _nonempty(cfg.get("ollama_essay_model")) or _a.ESSAY_MODEL},
            "fallback_local": False,
        }

    def one(name, flat, default):
        raw = block.get(name) if isinstance(block.get(name), dict) else {}
        provider = str(raw.get("provider") or "ollama").strip().lower()
        if provider not in PROVIDERS:
            provider = "ollama"
        # CLI tiers use the CLI's own default when no model was saved.
        # Do not copy the Ollama flat id into --model.
        if provider in CLI_PROVIDERS:
            model = _nonempty(raw.get("model"))
        else:
            model = _nonempty(raw.get("model")) or _nonempty(cfg.get(flat)) or default
        return {"provider": provider, "model": model}

    return {
        "answers": one("answers", "ollama_model", _a.MODEL),
        "essays": one("essays", "ollama_essay_model", _a.ESSAY_MODEL),
        "fallback_local": block.get("fallback_local") is True,
    }


def resolved_config():
    import jobs_store
    return _resolve(jobs_store.load_config())


def infer_task(model):
    """None or the answers model -> answers. The essay model -> essays."""
    import apply as _a
    if model is None or model == _a.MODEL:
        return "answers"
    if model == _a.ESSAY_MODEL:
        return "essays"
    return "answers"


def _cli_bins(cfg=None):
    """Binary names for the local CLIs. Defaults match a normal install."""
    if cfg is None:
        import jobs_store
        cfg = jobs_store.load_config()
    cfg = cfg if isinstance(cfg, dict) else {}
    block = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    raw = block.get("cli") if isinstance(block.get("cli"), dict) else {}
    out = dict(_DEFAULT_CLI)
    for key in out:
        val = _nonempty(raw.get(key))
        if val:
            out[key] = val
    return out


def cli_found(cfg=None):
    bins = _cli_bins(cfg)
    return {
        "claude": shutil.which(bins["claude_bin"]) is not None,
        "grok": shutil.which(bins["grok_bin"]) is not None,
    }


def public_view():
    import jobs_store
    raw = jobs_store.load_config()
    cfg = _resolve(raw)
    return {
        "ok": True,
        "answers": {"provider": cfg["answers"]["provider"], "model": cfg["answers"]["model"]},
        "essays": {"provider": cfg["essays"]["provider"], "model": cfg["essays"]["model"]},
        "fallback_local": cfg["fallback_local"],
        "key_set": {"anthropic": key_set("anthropic"), "xai": key_set("xai")},
        "cli_found": cli_found(raw),
    }


def _clean_tier(obj):
    if not isinstance(obj, dict):
        raise SettingsError("tier must be an object")
    out = {}
    if "provider" in obj:
        provider = obj.get("provider")
        if not isinstance(provider, str) or provider.strip().lower() not in PROVIDERS:
            raise SettingsError("provider must be ollama, anthropic, xai, claude_cli, or grok_cli")
        out["provider"] = provider.strip().lower()
    if "model" in obj:
        model = obj.get("model")
        if not isinstance(model, str) or len(model.strip()) > 200:
            raise SettingsError("invalid model")
        if not model.strip() and out.get("provider", obj.get("provider")) not in ("claude_cli", "grok_cli"):
            raise SettingsError("invalid model")   # blank means "CLI default" and only exists for CLI tiers
        if any(ord(ch) < 32 for ch in model):
            raise SettingsError("invalid model")
        out["model"] = model.strip()
    if not out:
        raise SettingsError("tier needs provider or model")
    return out


def _clean_key(value):
    if value is None:
        return None
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return _UNCHANGED
        if len(text) > 500 or any(ch in text for ch in "\r\n\t"):
            raise SettingsError("invalid api key")
        return text
    raise SettingsError("invalid api key")


def _clean_cli(obj):
    if not isinstance(obj, dict):
        raise SettingsError("cli must be an object")
    out = {}
    for key in ("claude_bin", "grok_bin"):
        if key not in obj:
            continue
        val = obj.get(key)
        if not isinstance(val, str):
            raise SettingsError("invalid cli binary")
        text = val.strip()
        if not text or len(text) > 260 or any(ord(ch) < 32 for ch in text):
            raise SettingsError("invalid cli binary")
        out[key] = text
    if not out:
        raise SettingsError("cli needs claude_bin or grok_bin")
    return out


def _save_llm_block(tier_updates, fallback_local, cli_update=None):
    import jobs_store
    cfg = jobs_store.load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    raw_block = cfg.get("llm") if isinstance(cfg.get("llm"), dict) else {}
    current = _resolve(cfg)
    block = {
        "answers": dict(current["answers"]),
        "essays": dict(current["essays"]),
        "fallback_local": current["fallback_local"] if fallback_local is None else fallback_local,
    }
    for name, upd in tier_updates.items():
        block[name].update(upd)
        raw_tier = raw_block.get(name) if isinstance(raw_block.get(name), dict) else {}
        explicit = _nonempty(raw_tier.get("model"))
        # A provider-only switch onto a CLI must not persist the synthesized
        # Ollama model id. An explicit model in the file or in this PUT stays.
        if block[name].get("provider") in CLI_PROVIDERS and "model" not in upd and not explicit:
            block[name]["model"] = ""
    if cli_update:
        cli = {}
        prev = raw_block.get("cli") if isinstance(raw_block.get("cli"), dict) else {}
        for key in ("claude_bin", "grok_bin"):
            prev_val = _nonempty(prev.get(key))
            if prev_val:
                cli[key] = prev_val
        cli.update(cli_update)
        block["cli"] = cli
    elif isinstance(raw_block.get("cli"), dict):
        block["cli"] = raw_block["cli"]
    cfg["llm"] = block
    if block["answers"]["provider"] == "ollama" and block["answers"].get("model"):
        cfg["ollama_model"] = block["answers"]["model"]
    if block["essays"]["provider"] == "ollama" and block["essays"].get("model"):
        cfg["ollama_essay_model"] = block["essays"]["model"]
    jobs_store.save_config(cfg)


def _splice_top_blocks(text, updates):
    """Replace only the named top-level keys. Other keys and comments stay."""
    lines = text.splitlines(keepends=True)
    top = re.compile(r"^([A-Za-z_][\w-]*)\s*:")
    for key, value in updates.items():
        dumped = yaml.safe_dump({key: value}, sort_keys=False, allow_unicode=True, width=100000)
        start = next((i for i, ln in enumerate(lines) if (m := top.match(ln)) and m.group(1) == key), None)
        if start is None:
            if lines and not lines[-1].endswith("\n"):
                lines[-1] += "\n"
            lines += ["\n"] + dumped.splitlines(keepends=True)
            continue
        end = len(lines)
        for j in range(start + 1, len(lines)):
            if top.match(lines[j]):
                end = j
                break
        k = end
        while k > start + 1 and (lines[k - 1].strip() == "" or lines[k - 1].lstrip().startswith("#")) and not lines[k - 1].startswith(" "):
            k -= 1
        lines[start:k] = dumped.splitlines(keepends=True)
    return "".join(lines)


def _merge_secret_keys(updates):
    """Merge top-level key fields. None clears. Other secrets.yaml keys stay."""
    path = secrets_path()
    text = ""
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            text = f.read()
    cur = {}
    if text.strip():
        loaded = yaml.safe_load(text) or {}
        if not isinstance(loaded, dict):
            raise SettingsError("could not update secrets")
        cur = loaded
    changed = {}
    for field, value in updates.items():
        if value is None:
            if cur.get(field) is not None:
                changed[field] = None
            continue
        if cur.get(field) != value:
            changed[field] = value
    if not changed:
        return
    new_text = _splice_top_blocks(text, changed)
    check = yaml.safe_load(new_text) or {}
    if not isinstance(check, dict):
        raise SettingsError("could not update secrets")
    for key, value in changed.items():
        if check.get(key) != value:
            raise SettingsError("could not update secrets")
    for key in cur:
        if key not in changed and check.get(key) != cur.get(key):
            raise SettingsError("could not update secrets")
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="") as f:
        f.write(new_text)
    os.replace(tmp, path)


def apply_settings(payload):
    """Merge a PUT body into config.json and, when asked, secrets.yaml.

    Returns True when a file changed. Blank key strings are a no-op.
    """
    if not isinstance(payload, dict):
        raise SettingsError("bad json")
    tier_updates = {}
    for name in ("answers", "essays"):
        if name in payload:
            tier_updates[name] = _clean_tier(payload[name])
    fallback_local = None
    if "fallback_local" in payload:
        if not isinstance(payload.get("fallback_local"), bool):
            raise SettingsError("fallback_local must be true or false")
        fallback_local = payload["fallback_local"]
    cli_update = None
    if "cli" in payload:
        cli_update = _clean_cli(payload["cli"])
    key_ops = {}
    saw_key_field = False
    for field in ("anthropic_api_key", "xai_api_key"):
        if field not in payload:
            continue
        saw_key_field = True
        cleaned = _clean_key(payload[field])
        if cleaned is not _UNCHANGED:
            key_ops[field] = cleaned
    if not tier_updates and fallback_local is None and not key_ops and cli_update is None:
        if saw_key_field:
            return False
        raise SettingsError("nothing to save")
    if tier_updates or fallback_local is not None or cli_update is not None:
        _save_llm_block(tier_updates, fallback_local, cli_update)
    if key_ops:
        _merge_secret_keys(key_ops)
    return True


def _local_model(task):
    import apply as _a
    import jobs_store
    cfg = jobs_store.load_config()
    if not isinstance(cfg, dict):
        cfg = {}
    if task == "essays":
        return _nonempty(cfg.get("ollama_essay_model")) or _a.ESSAY_MODEL
    return _nonempty(cfg.get("ollama_model")) or _a.MODEL


def _pick_model(provider, task, model, tier_model):
    import apply as _a
    if provider == "ollama":
        return _nonempty(model) or _nonempty(tier_model) or (_a.ESSAY_MODEL if task == "essays" else _a.MODEL)
    if provider in CLI_PROVIDERS:
        # Empty means the CLI's own default. seed and temperature are not used.
        return _nonempty(tier_model)
    chosen = _nonempty(tier_model)
    if not chosen:
        raise RuntimeError("model is not configured")
    return chosen


def _open(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(), headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as resp:
            raw = resp.read()
    except Exception as exc:
        extra = ""
        read = getattr(exc, "read", None)
        if callable(read):
            try:
                extra = read().decode("utf-8", "replace")[:800]
            except Exception:
                extra = ""
        close = getattr(exc, "close", None)
        if callable(close):
            try:
                close()
            except Exception:
                pass
        detail = str(exc)
        if extra:
            detail = detail + " " + extra
        raise RuntimeError(scrub_text(detail)) from None
    try:
        return json.loads(raw.decode("utf-8"))
    except Exception:
        raise RuntimeError("model returned non-json") from None


def _text_from_openai(data):
    try:
        return str(data["choices"][0]["message"]["content"]).strip()
    except Exception:
        raise RuntimeError("empty model response") from None


def _call_ollama(prompt, system, temperature, model, seed):
    import apply as _a
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": msgs, "temperature": temperature, "stream": False}
    if seed is not None:
        body["seed"] = int(seed)
        body["options"] = {"seed": int(seed)}
    return _text_from_openai(_open(_a.OLLAMA_BASE, body, {"Content-Type": "application/json"}))


def _call_xai(prompt, system, temperature, model):
    key = api_key("xai")
    if not key:
        raise RuntimeError("xai api key is not set")
    msgs = []
    if system:
        msgs.append({"role": "system", "content": system})
    msgs.append({"role": "user", "content": prompt})
    body = {"model": model, "messages": msgs, "temperature": temperature, "stream": False}
    headers = {"Content-Type": "application/json", "Authorization": "Bearer " + key}
    return _text_from_openai(_open(XAI_URL, body, headers))


def _call_anthropic(prompt, system, temperature, model):
    key = api_key("anthropic")
    if not key:
        raise RuntimeError("anthropic api key is not set")
    body = {
        "model": model,
        "max_tokens": MAX_TOKENS,
        "temperature": temperature,
        "messages": [{"role": "user", "content": prompt}],
    }
    if system:
        body["system"] = system
    headers = {
        "Content-Type": "application/json",
        "x-api-key": key,
        "anthropic-version": ANTHROPIC_VERSION,
    }
    data = _open(ANTHROPIC_URL, body, headers)
    try:
        return str(data["content"][0]["text"]).strip()
    except Exception:
        raise RuntimeError("empty model response") from None


def _strip_ansi(text):
    return _ANSI_RE.sub("", str(text or "")).strip()


def _claude_argv(bin_path, system, model):
    # Checked against `claude --help` on this box (Claude Code 2.1.274).
    # --tools "" disables every built-in tool. --bare is not used: it ignores
    # Max-plan OAuth and would require an API key.
    argv = [
        bin_path, "-p",
        "--output-format", "text",
        "--tools", "",
        "--permission-mode", "dontAsk",
        "--permission-prompts", "none",
        "--strict-mcp-config",
        "--no-session-persistence",
    ]
    if system and str(system).strip():
        argv.extend(["--system-prompt", system])
    if model:
        argv.extend(["--model", model])
    return argv


def _grok_argv(bin_path, prompt, system, model):
    # Checked against `grok --help` on this box. No stdin prompt flag;
    # --prompt-file exists, but the ticket asks for -p when stdin is absent.
    # --tools "" is the allowlist form (empty = no built-in tools).
    # dontAsk denies anything that is not already pre-approved.
    argv = [
        bin_path, "-p", prompt,
        "--output-format", "plain",
        "--tools", "",
        "--max-turns", "1",
        "--no-subagents",
        "--disable-web-search",
        "--permission-mode", "dontAsk",
        "--verbatim",
    ]
    if system and str(system).strip():
        argv.extend(["--system-prompt", system])
    if model:
        argv.extend(["--model", model])
    return argv


def _run_cli(provider, argv, prompt):
    """Run a CLI in an empty temp directory. No shell. Kill on timeout."""
    label = "claude" if provider == "claude_cli" else "grok"
    stdin_text = prompt if provider == "claude_cli" else None
    with tempfile.TemporaryDirectory(prefix="penates-cli-") as cwd:
        try:
            proc = subprocess.run(
                argv,
                input=stdin_text,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=CLI_TIMEOUT,
                cwd=cwd,
                shell=False,
            )
        except subprocess.TimeoutExpired:
            raise RuntimeError("%s CLI timed out after %ss" % (label, CLI_TIMEOUT)) from None
        except OSError as exc:
            raise RuntimeError("%s CLI failed to start: %s" % (label, exc.strerror or "os error")) from None
        if proc.returncode != 0:
            # Claude Code prints auth failures on stdout and leaves stderr empty.
            raw = proc.stderr or ""
            if not str(raw).strip():
                raw = proc.stdout or ""
            tail = scrub_text(_strip_ansi(raw)[-400:])
            raise RuntimeError("%s CLI exited %s: %s" % (label, proc.returncode, tail or "no stderr"))
        text = _strip_ansi(proc.stdout or "")
        if not text:
            raise RuntimeError("empty model response")
        return text


def _windows_shim_exe(path):
    """On Windows, use the exe an npm .cmd/.bat shim launches when it exists.

    cmd.exe mishandles quotes in --system-prompt, so a < becomes a redirect.
    The Claude Code shim runs node_modules/@anthropic-ai/claude-code/bin/<stem>.exe
    next to the shim. A missing exe keeps the shim path.
    """
    if os.name != "nt" or not path:
        return path
    low = path.lower()
    if not (low.endswith(".cmd") or low.endswith(".bat")):
        return path
    stem = os.path.splitext(os.path.basename(path))[0]
    exe = os.path.join(
        os.path.dirname(path),
        "node_modules", "@anthropic-ai", "claude-code", "bin", stem + ".exe",
    )
    if os.path.isfile(exe):
        return exe
    return path


def _require_cli(provider):
    bins = _cli_bins()
    key = "claude_bin" if provider == "claude_cli" else "grok_bin"
    label = "claude" if provider == "claude_cli" else "grok"
    name = bins[key]
    path = shutil.which(name)
    if not path:
        raise RuntimeError("%s CLI not found: %s" % (label, name))
    return _windows_shim_exe(path)


def _call_claude_cli(prompt, system, model):
    path = _require_cli("claude_cli")
    return _run_cli("claude_cli", _claude_argv(path, system, model), prompt)


def _call_grok_cli(prompt, system, model):
    path = _require_cli("grok_cli")
    return _run_cli("grok_cli", _grok_argv(path, prompt, system, model), prompt)


def _dispatch(provider, prompt, system, temperature, model, seed):
    if provider == "ollama":
        return _call_ollama(prompt, system, temperature, model, seed)
    if provider == "xai":
        return _call_xai(prompt, system, temperature, model)
    if provider == "anthropic":
        return _call_anthropic(prompt, system, temperature, model)
    if provider == "claude_cli":
        return _call_claude_cli(prompt, system, model)
    if provider == "grok_cli":
        return _call_grok_cli(prompt, system, model)
    raise RuntimeError("unknown provider")


def complete(prompt, system="", temperature=0.2, model=None, seed=None, task=None):
    """Return the assistant text. Cloud tiers use their configured model id."""
    global last_meta
    cfg = resolved_config()
    if task not in ("answers", "essays"):
        task = infer_task(model)
    tier = cfg[task]
    provider = tier["provider"]
    try:
        use_model = _pick_model(provider, task, model, tier.get("model"))
        last_meta = {"provider": provider, "fell_back": False, "model": use_model}
        return _dispatch(provider, prompt, system or "", temperature, use_model, seed)
    except Exception as exc:
        safe = scrub_text(exc)
        if provider != "ollama" and cfg.get("fallback_local"):
            try:
                fb_model = _local_model(task)
                text = _call_ollama(prompt, system or "", temperature, fb_model, seed)
                last_meta = {"provider": "ollama", "fell_back": True, "model": fb_model}
                return text
            except Exception as fb_exc:
                last_meta = {"provider": provider, "fell_back": False, "model": tier.get("model") or ""}
                raise RuntimeError(safe + "; local fallback failed: " + scrub_text(fb_exc)) from None
        last_meta = {"provider": provider, "fell_back": False, "model": tier.get("model") or ""}
        raise RuntimeError(safe) from None
