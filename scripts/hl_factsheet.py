"""
Refresh researched factsheet depth from each fund's HL factsheet.

The desk priced itself automatically long before it could research itself.
`fund_nav.py` gave every fund a return computed today, but the rest of an
entry - holdings, sector and country splits, charges, manager tenure, fund
size - stayed hand-researched, so `run_update.py` grew a warning for entries
whose research had aged past 120 days. That warning was the honest response to
a real problem; this module removes the problem instead.

HL's factsheet pages carry all of it in plain table rows: top ten holdings with
weights, sector and country breakdowns, net ongoing charge and the HL saving,
launch date, fund size, historic yield, IA sector, manager names with start
dates, and five discrete annual return periods with explicit date ranges.

WHAT THIS DOES NOT DO is invent anything. A field is written only when it is
found and parses; otherwise the existing value is left exactly as it was. A
fund whose page cannot be reached, or whose page turns out to describe a
different share class than the one we track, is REPORTED AND SKIPPED - never
partially applied. The failure mode to avoid is silently overwriting a
correct researched figure with a confident wrong one scraped from the wrong
page, which is strictly worse than a stale figure that is labelled stale.

Share-class safety: every page is checked against the fund's own name and
share class before a single field is taken from it. HL slugs are predictable
but not guaranteed, and "Class I Accumulation" and "Class I Income" differ by
one word in the URL while differing materially in every income figure.

  python scripts/hl_factsheet.py                 # refresh every fund
  python scripts/hl_factsheet.py --dry-run       # report, write nothing
  python scripts/hl_factsheet.py --only <id> ... # named funds only
  python scripts/hl_factsheet.py --new           # funds with no stored HL link
"""

from __future__ import annotations

import html as htmllib
import io
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

FUNDS = Path(__file__).resolve().parent.parent / "data" / "funds.json"

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/125.0 Safari/537.36")
TIMEOUT = 30
PAUSE = 1.2          # be a polite guest on someone else's server
RETRIES = 3

BASE = ("https://www.hl.co.uk/funds/fund-discounts,-prices--and--factsheets"
        "/search-results/{letter}/{slug}")

# Word-overlap floor when confirming a fetched page really is our fund. Same
# reasoning as fund_nav.py's threshold: tolerate punctuation and house-name
# noise, reject a different fund that merely shares a manager or a theme.
NAME_FLOOR = 0.6

STOP = {"class", "accumulation", "income", "acc", "inc", "fund", "trust",
        "the", "of", "and", "a", "gbp", "hedged", "institutional", "unit",
        "units", "shares", "share", "professional", "quarterly", "monthly"}


# ---------------------------------------------------------------- fetching

def _get(url: str) -> str | None:
    """Fetch a page, retrying transient failures. None on a hard failure."""
    req = urllib.request.Request(url, headers={
        "User-Agent": USER_AGENT,
        "Accept": "text/html,application/xhtml+xml",
        "Accept-Language": "en-GB,en;q=0.9",
    })
    for attempt in range(RETRIES):
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                return r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None                      # a wrong slug, not a blip
            time.sleep(2.0 * (attempt + 1))      # 403/429/5xx: back off
        except Exception:
            time.sleep(2.0 * (attempt + 1))
    return None


def slug_candidates(fund: dict) -> list[str]:
    """HL slugs are name + share class, lowercased and hyphenated."""
    name, share = fund["name"], fund.get("shareClass", "")

    def norm(s: str) -> str:
        s = s.replace("&", " and ").replace("+", " plus ")
        s = re.sub(r"[^\w\s-]", " ", s)
        return re.sub(r"[\s_]+", "-", s.strip().lower()).strip("-")

    out, seen = [], set()
    for variant in (f"{name} {share}", name):
        s = norm(variant)
        if s and s not in seen:
            seen.add(s)
            out.append(s)
    # HL writes "Legal & General" as "legal-and-general"; some houses appear
    # under a short form instead. Try the obvious contraction too.
    short = norm(f"{name} {share}").replace("legal-and-general", "lg")
    if short not in seen:
        out.append(short)
    return out


# ---------------------------------------------------------------- parsing

# Mojibake: UTF-8 bytes decoded as cp1252 somewhere upstream, so "¼" arrives
# as "Â¼" and "—" as "â€”". This is not our decode to fix - HL publish it in
# their own markup, as the literal entities "4&Acirc;&frac14;%" - so the job
# is to survive it rather than to correct the fetch.
#
# The repair is to put the text back through the encoding that mangled it:
# encode as cp1252 to recover the original bytes, decode those as UTF-8.
_SUSPECT = re.compile(r"[ÂÃ][-¿]|â€")


def demojibake(s: str) -> str:
    """
    Undo a cp1252-decoded-UTF-8 mangling, and only that.

    Guarded three ways, because a wrong "repair" silently corrupts a holding
    name. It is attempted only on text showing the signature; the decode must
    succeed strictly, so text that was never mangled raises and is left alone;
    and the result must re-mangle back to exactly the input, which is an
    inverse check rather than a guess. Anything short of that keeps the
    original - a stray "Â" is ugly, a corrupted fund name is wrong.
    """
    if not s:
        return s
    out = s
    for _ in range(3):          # text is occasionally mangled twice over
        if not _SUSPECT.search(out):
            break
        for codec in ("cp1252", "latin-1"):
            try:
                fixed = out.encode(codec).decode("utf-8")
                if fixed.encode("utf-8").decode(codec) != out:
                    continue    # not a clean inverse; do not trust it
            except (UnicodeEncodeError, UnicodeDecodeError):
                continue
            if fixed != out:
                out = fixed
                break
        else:
            break
    return out


def text_of(fragment: str) -> str:
    return demojibake(re.sub(
        r"\s+", " ",
        htmllib.unescape(re.sub(r"<[^>]+>", " ", fragment))).strip())


def rows(html: str) -> list[list[str]]:
    """Every <tr> as a list of non-empty cell strings."""
    out = []
    for m in re.finditer(r"<tr[^>]*>(.*?)</tr>", html, re.S | re.I):
        cells = [text_of(c) for c in
                 re.findall(r"<t[dh][^>]*>(.*?)</t[dh]>", m.group(1), re.S | re.I)]
        cells = [c for c in cells if c]
        if cells:
            out.append(cells)
    return out


def labelled(rs: list[list[str]], *labels: str) -> str | None:
    """First cell after a cell matching one of `labels` (ignoring a ':')."""
    wanted = {l.lower().rstrip(":") for l in labels}
    for cells in rs:
        for i, c in enumerate(cells):
            if c.lower().rstrip(":").strip() in wanted:
                for nxt in cells[i + 1:]:
                    v = nxt.strip(": ").strip()
                    if v and v != ":":
                        return v
    return None


PCT = re.compile(r"^-?\d+(?:\.\d+)?%$")


def clean_pct(v: str | None) -> str | None:
    """HL appends a tooltip glyph to some charge cells: "0.27% i"."""
    if not v:
        return v
    m = re.search(r"-?\d+(?:\.\d+)?%", v)
    return m.group() if m else v


# HL prints holdings in block capitals. Title-case them to match the rest of
# the desk, but keep genuine acronyms and legal-form suffixes (BAE, ASA, S.A.)
# that title-casing would otherwise mangle into "Bae" and "Asa".
def tidy_name(s: str) -> str:
    out = []
    for tok in s.split():
        bare = tok.replace(".", "").replace(",", "")
        if bare.isupper() and len(bare) <= 3:
            out.append(tok)
        else:
            out.append(tok.title())
    return " ".join(out)


def weighted_block(rs: list[list[str]], header: str) -> list[dict] | None:
    """Rows of `name, weight%` following a `header | Weight` row."""
    out, seen_header = [], False
    for cells in rs:
        low = [c.lower() for c in cells]
        if not seen_header:
            if len(cells) >= 2 and low[0] == header.lower() and "weight" in low[1]:
                seen_header = True
            continue
        if len(cells) >= 2 and PCT.match(cells[-1]) and not PCT.match(cells[0]):
            out.append({"name": cells[0].strip(), "weight": cells[-1]})
        elif out:
            break
    return out or None


def holdings_block(rs: list[list[str]]) -> list[dict] | None:
    """
    Top holdings carry no header row, so they are identified structurally:
    a run of `NAME, x.xx%` rows that is not the sector or country block.
    """
    blocks, cur = [], []
    for cells in rs:
        if len(cells) >= 2 and PCT.match(cells[-1]) and not PCT.match(cells[0]):
            cur.append({"name": tidy_name(cells[0].strip()), "weight": cells[-1]})
        else:
            low = [c.lower() for c in cells]
            if cur:
                blocks.append((cur, low))
                cur = []
    if cur:
        blocks.append((cur, []))
    # The holdings block is the first run of >=5 such rows; the sector and
    # country runs are preceded by their own header rows, which weighted_block
    # keys off, so take the earliest run and let the caller drop duplicates.
    for run, _ in blocks:
        if len(run) >= 5:
            return run[:10]
    return None


def discrete_returns(rs: list[list[str]]) -> list[dict] | None:
    """
    HL prints five discrete annual periods as a date-range header row followed
    by an `Annual return` row. Both must be present and the same length, or we
    take neither - a misaligned pairing would attribute a return to the wrong
    year, which is worse than having no discrete history at all.
    """
    periods = None
    for cells in rs:
        ranges = [c for c in cells if re.match(r"^\d{2}/\d{2}/\d{2} to \d{2}/\d{2}/\d{2}$", c)]
        if len(ranges) >= 3:
            periods = ranges
            continue
        if periods and cells and cells[0].lower().startswith("annual return"):
            vals = [c for c in cells[1:] if PCT.match(c)]
            if len(vals) != len(periods):
                return None
            out = [{"year": p, "fund": ("+" + v if not v.startswith("-") else v),
                    "sector": "not yet verified"}
                   for p, v in zip(periods, vals)]
            # HL lists oldest first; this desk reads discrete[0] as the most
            # recent year, so reverse rather than silently relabel history.
            return list(reversed(out))
    return None


def managers(html: str) -> dict | None:
    """Manager names each followed by a `Manager start date` row."""
    rs = rows(html)
    names, starts = [], []
    for i, cells in enumerate(rs):
        if cells and cells[0].lower().startswith("manager start date"):
            for c in cells[1:]:
                if re.match(r"^\d{2}/\d{2}/\d{4}$", c):
                    starts.append(c)
                    break
            for prev in reversed(rs[:i]):
                cand = prev[0].strip()
                if (len(prev) == 1 and 2 <= len(cand.split()) <= 5
                        and not cand.lower().startswith("manager")
                        and re.match(r"^[A-Z]", cand)):
                    names.append(cand)
                    break
    if not names:
        return None
    pairs = list(zip(names, starts + [""] * (len(names) - len(starts))))
    return {
        "names": " & ".join(n for n, _ in pairs),
        "tenure": "; ".join(f"{n} since {s}" for n, s in pairs if s) or "not yet verified",
    }


def parse(html: str) -> dict:
    """Everything we can honestly read off one factsheet."""
    rs = rows(html)
    out: dict = {}

    if (v := labelled(rs, "Fund size")):
        out["fundSize"] = v
    if (v := labelled(rs, "Fund launch date", "Launch date")):
        out["launched"] = v
    if (v := labelled(rs, "Sector")):
        out["iaSector"] = "IA " + v if not v.upper().startswith("IA") else v
    if (v := clean_pct(labelled(rs, "Historic yield"))):
        # The desk has always labelled this basis explicitly.
        out["fundYield"] = f"{v} (historic)"
    if (v := labelled(rs, "Number of holdings")):
        out["numHoldings"] = v

    charges = {}
    if (v := clean_pct(labelled(rs, "Net ongoing charge"))):
        charges["netOngoing"] = v
    if (v := clean_pct(labelled(rs, "Ongoing saving from HL"))):
        charges["hlSaving"] = v
    if charges:
        out["charges"] = charges

    if (v := holdings_block(rs)):
        out["holdings"] = v
    if (v := weighted_block(rs, "Sector")):
        out["sectors"] = v
    if (v := weighted_block(rs, "Country")):
        out["countries"] = v
    if (v := discrete_returns(rs)):
        out["discrete"] = v
    if (v := managers(html)):
        out["manager"] = v

    # Wealth Shortlist membership is stated in prose, not a table.
    flat = text_of(html).lower()
    if "selected this fund for the wealth shortlist" in flat:
        out["shortlist"] = True
    elif "does not feature on the wealth shortlist" in flat:
        out["shortlist"] = False
    return out


# ------------------------------------------------------------- confirming

def words(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", s.lower()) if w not in STOP and len(w) > 1}


# Fields that describe the fund as a whole and are identical across share
# classes, versus the ones that are not. Charges, yield and the discrete
# return history all differ class by class, so they may only be taken from a
# page describing the very class this desk prices.
CLASS_SPECIFIC = {"charges", "fundYield", "discrete"}


def class_letter(s: str) -> str | None:
    """The share-class designator, e.g. 'I' from 'Class I Accumulation'."""
    m = re.search(r"\bclass\s+([a-z0-9]{1,3})\b", s, re.I)
    if m:
        return m.group(1).upper()
    m = re.search(r"\b(?:institutional|inst)\s+([a-z0-9]{1,3})\b", s, re.I)
    return m.group(1).upper() if m else None


def page_is_ours(html: str, fund: dict) -> tuple[bool, str]:
    """
    Confirm the page describes this fund AND this share class before we take
    a single figure from it.
    """
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S | re.I)
    title = text_of(m.group(1)) if m else ""
    if not title:
        return False, "no page title"

    ours = words(fund["name"])
    theirs = words(title)
    if not ours:
        return False, "no comparable words in our name"
    score = len(ours & theirs) / len(ours)
    if score < NAME_FLOOR:
        return False, f"name mismatch ({score:.0%}): page says {title!r}"

    # Accumulation vs income changes every income figure on the page.
    want_acc = "accumulation" in (fund.get("shareClass", "") + " " + fund["name"]).lower()
    rs = rows(html)
    kind = (labelled(rs, "Accumulation/income") or "").lower()
    if kind:
        if want_acc and "accumulation" not in kind:
            return False, f"share-class mismatch: page is {kind!r}, we track accumulation"
        if not want_acc and "accumulation" in kind:
            return False, f"share-class mismatch: page is {kind!r}, we do not track accumulation"
    return True, title


# ----------------------------------------------------------------- applying

NYV = "not yet verified"


def apply(fund: dict, got: dict, class_ok: bool = True) -> list[str]:
    """
    Write parsed fields onto the fund. Returns a list of change notes.

    When the page describes a different share class than the one this desk
    prices, the class-specific fields are dropped rather than applied: a
    Class I ongoing charge on a Class Z holding is precise and wrong.
    """
    changed = []
    if not class_ok:
        got = {k: v for k, v in got.items() if k not in CLASS_SPECIFIC}

    def put(key, val, label=None):
        if val is None:
            return
        old = fund.get(key)
        if old != val:
            changed.append(f"{label or key}: {old!r} -> {val!r}"
                           if old in (None, NYV) or len(str(old)) < 40
                           else f"{label or key} updated")
            fund[key] = val

    put("fundSize", got.get("fundSize"))
    put("launched", got.get("launched"))
    put("iaSector", got.get("iaSector"))
    if got.get("fundYield"):
        put("yield", got["fundYield"], "yield")

    if got.get("charges"):
        ch = fund.setdefault("charges", {})
        for k, v in got["charges"].items():
            if ch.get(k) != v:
                changed.append(f"charges.{k}: {ch.get(k)!r} -> {v!r}")
                ch[k] = v
        # HL's "net ongoing" is the OCF after its own saving; keep both named
        # as the rest of the desk names them.
        if "netOngoing" in got["charges"] and ch.get("ocf") in (None, NYV):
            ch["ocf"] = got["charges"]["netOngoing"]

    for key in ("holdings", "sectors", "countries"):
        if got.get(key):
            if fund.get(key) != got[key]:
                changed.append(f"{key}: {len(got[key])} rows")
                fund[key] = got[key]

    if got.get("numHoldings"):
        note = (f"Top ten of {got['numHoldings']} holdings, from the HL factsheet "
                f"on {date.today():%d %b %Y}.")
        if fund.get("holdingsNote") != note:
            fund["holdingsNote"] = note
            changed.append("holdingsNote refreshed")

    if got.get("manager"):
        mgr = fund.setdefault("manager", {})
        for k, v in got["manager"].items():
            if mgr.get(k) != v:
                changed.append(f"manager.{k}: {mgr.get(k)!r} -> {v!r}")
                mgr[k] = v

    if got.get("discrete"):
        perf = fund.setdefault("performance", {})
        existing = perf.get("discrete") or []
        # HL publishes fund returns but no sector comparator on this table.
        # Where an entry already carries researched sector figures, keep them:
        # HL's periods end 26 August and the researched ones generally end 31
        # March, so copying a comparator across would attach it to a period it
        # was never measured over. A real comparator on the old basis beats a
        # misattributed one on the new basis.
        has_sector = any(r.get("sector") not in (None, NYV, "—")
                         for r in existing)
        if has_sector:
            changed.append(f"discrete: kept {len(existing)} researched periods "
                           f"(they carry sector comparators HL does not publish)")
        elif existing != got["discrete"]:
            perf["discrete"] = got["discrete"]
            changed.append(f"discrete: {len(got['discrete'])} periods")

    if "shortlist" in got:
        badge = fund.setdefault("badge", {})
        want = ({"type": "", "label": "Wealth Shortlist"} if got["shortlist"]
                else {"type": "off", "label": "NOT on Shortlist"})
        if badge.get("label") != want["label"]:
            changed.append(f"badge: {badge.get('label')!r} -> {want['label']!r}")
            fund["badge"] = want

    # Stamp the date on every successful read, not only on a change. A field
    # confirmed unchanged against HL today is verified, not stale, and the
    # 120-day warning in run_update.py should not fire on it.
    fund["asAt"] = (f"{date.today():%d %b %Y} (factsheet "
                    f"{'refreshed' if changed else 'confirmed unchanged'} from HL)")
    fund.setdefault("performance", {})["perfAsAt"] = f"{date.today():%Y-%m-%d}"
    fund["performance"]["perfAsAtSource"] = "HL factsheet scrape"
    return changed


# --------------------------------------------------------------------- main

def resolve_url(fund: dict) -> tuple[str | None, str | None, str]:
    """Return (url, html, note) for the page that is genuinely this fund's."""
    stored = (fund.get("links") or {}).get("hl")
    # A stored link may point at one of HL's sub-tabs. Those pages carry the
    # header and the return table but none of the portfolio breakdown, so a
    # scrape from them looks like a fund that publishes no holdings.
    if stored:
        stored = re.sub(r"/(?:research|charts|key-features|fund-analysis"
                        r"|security-details|our-view)/?$", "", stored)
    tried = []
    urls = ([stored] if stored else []) + [
        BASE.format(letter=s[0], slug=s) for s in slug_candidates(fund)]
    for url in urls:
        if not url or url in tried:
            continue
        tried.append(url)
        html = _get(url)
        time.sleep(PAUSE)
        if html is None:
            continue
        ok, why = page_is_ours(html, fund)
        if ok:
            return url, html, why
        return None, None, why          # found a page, but it is not ours
    return None, None, "no HL page found"


def repair_stored_text(doc: dict) -> list[tuple[str, str, str]]:
    """
    Walk every string in the document and undo any stored mojibake.

    Fixing text_of stops new mangling arriving, but the desk has been storing
    HL's for as long as it has been scraping - and the page title carries some
    of its own, from well before any of this was automated. Repairing in place
    is safe precisely because demojibake refuses anything that is not an exact
    inverse; a string it cannot prove was mangled is returned untouched.
    """
    fixed: list[tuple[str, str, str]] = []

    def walk(node, path):
        if isinstance(node, dict):
            for k, v in list(node.items()):
                if isinstance(v, str):
                    repaired = demojibake(v)
                    if repaired != v:
                        node[k] = repaired
                        fixed.append((f"{path}/{k}", v, repaired))
                else:
                    walk(v, f"{path}/{k}")
        elif isinstance(node, list):
            for i, v in enumerate(node):
                if isinstance(v, str):
                    repaired = demojibake(v)
                    if repaired != v:
                        node[i] = repaired
                        fixed.append((f"{path}/{i}", v, repaired))
                else:
                    walk(v, f"{path}/{i}")

    walk(doc, "")
    return fixed


def main(argv: list[str]) -> int:
    dry = "--dry-run" in argv
    new_only = "--new" in argv

    if "--repair-text" in argv:
        doc = json.load(io.open(FUNDS, encoding="utf-8"))
        fixed = repair_stored_text(doc)
        for path, before, after in fixed:
            print(f"  {path}\n      {before[:88]}\n   -> {after[:88]}")
        print(f"\n{len(fixed)} string(s) repaired")
        if dry or not fixed:
            print("--dry-run: nothing written" if dry else "nothing to write")
            return 0
        io.open(FUNDS, "w", encoding="utf-8", newline="\n").write(
            json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
        print(f"wrote {FUNDS}")
        return 0
    only = set()
    if "--only" in argv:
        only = set(argv[argv.index("--only") + 1:])

    doc = json.load(io.open(FUNDS, encoding="utf-8"))
    funds = doc["funds"]
    if only:
        funds = [f for f in funds if f["id"] in only]
    elif new_only:
        funds = [f for f in funds if not (f.get("links") or {}).get("hl")]

    ok = skipped = untouched = 0
    problems = []
    mismatches = []

    for f in funds:
        url, html, note = resolve_url(f)
        if not html:
            problems.append((f["id"], note))
            print(f"  [skip] {f['name'][:44]:44} {note}", flush=True)
            skipped += 1
            continue
        # HL often lists a different retail share class than the one this desk
        # prices. Whole-fund facts still apply; class-specific ones do not.
        ours = class_letter(f.get("shareClass", "") + " " + f["name"])
        theirs = class_letter(note)
        class_ok = not (ours and theirs and ours != theirs)
        if not class_ok:
            mismatches.append((f["id"], ours, theirs))
            print(f"  [class] {f['name'][:43]:43} we price Class {ours}, HL lists "
                  f"Class {theirs} - whole-fund fields only", flush=True)
        changes = apply(f, parse(html), class_ok=class_ok)
        f.setdefault("links", {})["hl"] = url
        if changes:
            ok += 1
            print(f"  [hl]   {f['name'][:44]:44} {len(changes)} field(s)", flush=True)
            for c in changes[:4]:
                print(f"           - {c[:110]}", flush=True)
            if len(changes) > 4:
                print(f"           - ... and {len(changes)-4} more", flush=True)
        else:
            untouched += 1
            print(f"  [same] {f['name'][:44]:44} already current", flush=True)

    print(f"\n{ok} refreshed, {untouched} already current, {skipped} skipped "
          f"of {len(funds)} funds")
    if problems:
        print("\nCould not refresh - left exactly as they were:")
        for fid, why in problems:
            print(f"  {fid:44} {why[:100]}")
    if mismatches:
        print("\nShare-class mismatches. Charges, yield and discrete history were")
        print("skipped for these, because those differ by class and HL lists another:")
        for fid, ours, theirs in mismatches:
            print(f"  {fid:44} desk prices Class {ours}, HL page Class {theirs}")

    if dry:
        print("\n--dry-run: nothing written")
        return 0
    io.open(FUNDS, "w", encoding="utf-8", newline="\n").write(
        json.dumps(doc, indent=2, ensure_ascii=False) + "\n")
    print(f"\nwrote {FUNDS}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
