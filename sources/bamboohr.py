"""BambooHR public careers list: https://{slug}.bamboohr.com/careers/list
locationType: "0" on-site, "1" remote, "2" hybrid (checked against the rendered job page)."""
from . import base
URL = "https://%s.bamboohr.com/careers/list"
JOB = "https://%s.bamboohr.com/careers/%s"

def fetch(board, company=None):
    data = base.http_json(URL % board)
    out = []
    for o in data.get("result", []) or []:
        a = o.get("atsLocation") or o.get("location") or {}
        loc = ", ".join(x for x in (a.get("city"), a.get("state") or a.get("province"), a.get("country")) if x)
        lt = str(o.get("locationType") or "")
        remote = True if (o.get("isRemote") is True or lt == "1") else (False if lt in ("0", "2") else None)
        if remote and "remote" not in loc.lower():
            loc = ("Remote - " + loc) if loc else "Remote"
        elif lt == "2":
            loc = ("Hybrid - " + loc) if loc else "Hybrid"
        out.append(base.job("bamboohr", board, o.get("id"), title=o.get("jobOpeningName"),
                            company=company or board, url=JOB % (board, o.get("id")),
                            location=loc, remote=remote))
    return out
