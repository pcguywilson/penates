"""BambooHR public careers list: https://{slug}.bamboohr.com/careers/list
locationType: "0" on-site, "1" remote, "2" hybrid (checked against the rendered job page)."""
import re
from . import base
URL = "https://%s.bamboohr.com/careers/list"
JOB = "https://%s.bamboohr.com/careers/%s"

def fetch(board, company=None):
    data = base.http_json(URL % board)
    out = []
    for o in data.get("result", []) or []:
        # atsLocation can be present with every value null (seen on several boards); fall back to location then.
        ats_loc = o.get("atsLocation") or {}
        a = ats_loc if any(ats_loc.values()) else (o.get("location") or {})
        loc = ", ".join(x for x in (a.get("city"), a.get("state") or a.get("province"), a.get("country")) if x)
        lt = str(o.get("locationType") or "")
        remote = True if (o.get("isRemote") is True or lt == "1") else (False if lt in ("0", "2") else None)
        if remote and "remote" not in loc.lower():
            loc = ("Remote - " + loc) if loc else "Remote"
        elif lt == "2":
            loc = ("Hybrid - " + loc) if loc else "Hybrid"
        row = base.job("bamboohr", board, o.get("id"), title=o.get("jobOpeningName"),
                       company=company or board, url=JOB % (board, o.get("id")),
                       location=loc, remote=remote)
        et = str(o.get("employmentStatusLabel") or o.get("employmentType") or "").lower()
        jt = ("contract" if re.search(r"contract|temp|1099|c2c", et) else
              "part_time" if "part" in et else "full_time" if "full" in et else "")
        if jt:
            row["job_type"] = jt
        out.append(row)
    return out
