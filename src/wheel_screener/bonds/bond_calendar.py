"""Event calendar for a watchlist of Russian bonds — a port of the standalone bond_calendar.py.

Pulls data from the Moscow Exchange (the open ISS API, no keys or sign-up) and builds a
"date — event" list: coupons, offers (the application window and the buyback itself),
amortizations and redemptions, as CSV and as an .ics calendar (opens in Apple/Google Calendar).

The MOEX logic is the script's, unchanged; what differs is the delivery: outputs are returned
as text instead of written to files, and ``build_calendar`` is the script's ``main()`` minus the
argument parsing and file handling, so the web app (``bonds.service``) can call it.

Watchlist format (either of two):
  • CSV with a header. Columns:
      code (or isin / secid / ticker) — ISIN or MOEX code;
      name — the bond name as MOEX and cbonds write it (in Russian):
        "issuer, series", e.g. AFK Sistema, 001P-06; if code is empty,
        the ISIN is looked up by name (and maturity date, if given);
      matdate — maturity date, helps find the right bond by name;
      qty (or quantity) — how many bonds you hold, to compute totals.
    Only one of the code or name columns is required. Russian column
    names (as in cbonds exports) are accepted too.
  • Plain text: one bond per line, optionally followed by a quantity.
    Anything after # is a comment.
  Both ISINs (RU000A1038V6) and MOEX codes (SU26238RMFS4) work.
  Matured bonds that MOEX no longer lists carry the code "-" once looked up, so they aren't
  looked up again. To look a bond up again, clear its code.

Bond names in the output are the names from the watchlist (MOEX's short name for bonds listed
by code only). Event types are in Russian, as MOEX and brokers call them: Купон, Амортизация,
Погашение, Оферта: выкуп, and so on.

Standard library only, like the script.
"""

from __future__ import annotations

import csv
import gzip
import hashlib
import http.client
import io
import json
import re
import threading
import time
from collections import defaultdict
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import urlencode

ISS_HOST = "iss.moex.com"
ISS = "/iss"
HEADERS = {"User-Agent": "bond-calendar/1.0", "Accept-Encoding": "gzip"}
# connecting usually takes ~0.6 s, but ~1% of attempts get no answer at all — don't wait
CONNECT_TIMEOUT = 5
# a response usually takes < 1 s; a stalled request never recovers — better to retry
TIMEOUT = 15
PAGE = 100     # how many rows to ask MOEX for per request
# cards per request: with more codes (~1000 characters) MOEX silently returns every bond
CARDS_CHUNK = 50

# accepted header names; the Russian ones are for exports such as cbonds
ID_COLUMNS = {"isin", "secid", "тикер", "код", "ticker", "code"}
NAME_COLUMNS = {"name", "название", "эмиссия", "бумага"}
MAT_COLUMNS = {"matdate", "дата погашения", "погашение"}
QTY_COLUMNS = {"qty", "quantity", "количество", "кол-во", "шт", "штук"}
# code in the saved watchlist: the bond has matured and MOEX doesn't list it — don't look it up
NOT_FOUND = "-"
CURRENCY = {"SUR": "RUB", "RUR": "RUB"}

COLUMNS = ["Date", "Event", "Bond", "ISIN", "Code", "Per bond",
           "Currency", "Coupon, % p.a.", "Qty", "Total", "Note"]

# event types are in Russian, as MOEX and brokers call them; they are also part of
# the .ics event IDs — renaming them makes calendar apps duplicate imported events
COUPON, AMORTIZATION, REDEMPTION = "Купон", "Амортизация", "Погашение"
OFFER_OPENS = "Оферта: начало приёма заявок"
OFFER_LAST_DAY = "Оферта: последний день заявок"
OFFER_DAY = "Оферта: день подачи заявок"
OFFER_BUYBACK = "Оферта: выкуп"

# .ics reminders: about the last day to apply for an offer — remind_days before,
# about a buyback whose application dates aren't announced yet — 14 days before
REMIND_WINDOW = "window"
REMIND_NO_WINDOW = 14


# ───────────────────────── small helpers ─────────────────────────

def to_number(x) -> float | None:
    if x is None or x == "":
        return None
    try:
        return float(str(x).replace(" ", "").replace(",", "."))
    except ValueError:
        return None


def parse_date(x) -> date | None:
    if not x or str(x).startswith("0000"):
        return None
    try:
        return date.fromisoformat(str(x)[:10])
    except ValueError:
        return None


def one_year_after(d: date) -> date:
    try:
        return d.replace(year=d.year + 1)
    except ValueError:          # February 29
        return d.replace(year=d.year + 1, day=28)


def currency_of(row: dict) -> str:
    unit = str(row.get("faceunit") or "").upper()
    return CURRENCY.get(unit, unit)


# ───────────────────────── watchlist ─────────────────────────

def parse_watchlist(text: str) -> list[dict]:
    """Returns a list of {code, name, matdate, qty}. code may be empty if only
    a name is given — then the ISIN is looked up by name."""
    lines = []
    for raw in text.lstrip("\ufeff").splitlines():
        line = raw.split("#", 1)[0].strip()
        if line:
            lines.append(line)
    if not lines:
        return []

    # the delimiter is whichever character occurs most in the first line
    delim = max(",;\t", key=lines[0].count)
    if lines[0].count(delim):
        rows = list(csv.reader(lines, delimiter=delim))
    else:
        rows = [line.split() for line in lines]

    header = [c.strip().lower() for c in rows[0]]
    def col(names):
        return next((i for i, c in enumerate(header) if c in names), None)
    id_col, name_col = col(ID_COLUMNS), col(NAME_COLUMNS)
    if id_col is None and name_col is None:  # no header: code, quantity
        id_col, name_col, mat_col, qty_col = 0, None, None, 1
    else:
        mat_col, qty_col = col(MAT_COLUMNS), col(QTY_COLUMNS)
        rows = rows[1:]

    def cell(row, i):
        return row[i].strip() if i is not None and i < len(row) else ""

    items: dict[str, dict] = {}
    for row in rows:
        code = cell(row, id_col).upper()
        name = cell(row, name_col)
        if not code and not name:
            continue
        qty = to_number(cell(row, qty_col))
        key = code if code and code != NOT_FOUND else name.lower()
        if key in items:                 # the same bond on several lines — add up
            old = items[key]
            if qty is not None:
                old["qty"] = (old["qty"] or 0) + qty
            continue
        items[key] = {"code": code, "name": name, "matdate": parse_date(cell(row, mat_col)),
                      "qty": qty}
    return list(items.values())


def read_watchlist(path: Path) -> list[dict]:
    return parse_watchlist(path.read_text(encoding="utf-8-sig"))


# ───────────────────── ISIN lookup by name ─────────────────────
# Names like "AFK Sistema, 001P-06" in Russian (as in cbonds exports): issuer and series.
# The Cyrillic below is matching data for MOEX's Russian bond names.

LATIN = str.maketrans("АВЕКМНОРСТУХ", "ABEKMHOPCTYX")   # Cyrillic letters that look like Latin
# legal forms and generic words that don't identify an issuer
STOP_WORDS = {"пао", "оао", "зао", "ооо", "группа", "групп", "компания",
              "государственная", "банк", "область", "край"}


def norm_series(s: str) -> str:
    return re.sub(r"\s+", "", s.upper().replace("Ё", "Е").translate(LATIN))


def series_variants(series: str) -> set[str]:
    """001P-06 → {001P-06, 1P-6, 001P06, 1P6}: the ways a series is written in different names."""
    s = norm_series(series)
    parts = s.split("-")
    short = [p.lstrip("0") or "0" for p in parts]
    return {v for v in {s, "-".join(short), "".join(parts), "".join(short)} if len(v) >= 2}


def issuer_words(issuer: str) -> list[str]:
    words = re.findall(r"[а-яa-z0-9]+", issuer.lower().replace("ё", "е"))
    # long words without their ending: "Ulyanovskaya" is also found in "Ulyanovskoy obl."
    return [w[:-2] if len(w) >= 7 else w
            for w in words if len(w) >= 3 and w not in STOP_WORDS]


def compact(s: str) -> str:
    return re.sub(r"[^а-яa-z0-9]", "", str(s or "").lower().replace("ё", "е"))


# one MOEX list for all names — normalize each bond only once (bounded: the server lives on)
@lru_cache(maxsize=50_000)
def search_forms(text: str) -> tuple[str, str]:
    """A candidate's name prepared for the series search (spaces kept — they are
    word boundaries) and compacted for the issuer-word search."""
    return text.upper().replace("Ё", "Е").translate(LATIN), compact(text)


def series_pattern(variants: set[str]) -> re.Pattern | None:
    if not variants:
        return None
    alts = "|".join(re.escape(v) for v in sorted(variants))
    # nothing may continue the series: BO-P01 ≠ BO-P01-USD, 001P-04 ≠ 001P-04R
    return re.compile(rf"(?<![0-9])(?:{alts})(?![0-9A-ZА-Я]|-[0-9A-ZА-Я])")


def score(cand: dict, issuer: list[str], series: re.Pattern | None) -> tuple[bool, float]:
    """(series matched, share of the issuer's words found in the name)."""
    text = " ".join(str(cand.get(k) or "") for k in ("secname", "name", "shortname", "latname"))
    t, ctext = search_forms(text)
    series_ok = bool(series and series.search(t))
    found = sum(1 for w in issuer if w in ctext)
    return series_ok, (found / len(issuer) if issuer else 0.0)


def pick(cands: list[dict], issuer: list[str], variants: set[str], need_series: bool,
         matdate: date | None = None) -> dict | None:
    """The best candidate, or None if nobody fits or two fit equally well.
    need_series=False — search among bonds with the same maturity date: a matching
    series is enough there (the issuer may be named differently: Gazprombank = GPB)."""
    best, best_key, tie = None, None, False
    series = series_pattern(variants)
    by_isin = {}
    for c in cands:
        by_isin.setdefault(c.get("isin") or c.get("secid"), c)
    for c in by_isin.values():
        series_ok, iss = score(c, issuer, series)
        if need_series and not series_ok:
            continue
        if iss == 0 and not (series_ok and not need_series):
            continue
        cand_mat = parse_date(c.get("matdate"))
        if matdate and cand_mat and cand_mat != matdate:   # another maturity — another bond
            continue
        key = (series_ok, iss)
        if best_key is None or key > best_key:
            best, best_key, tie = c, key, False
        elif key == best_key:
            tie = True
    return None if tie else best


def resolve_names(items: list[dict], log, skip_before: date | None = None, pmap=map) -> list[str]:
    """Fills in code for bonds that only have a name. Returns a list of problems.
    Bonds that matured before skip_before are skipped: they have no events in the period.
    pmap — how to go over the bonds: with pool.map the search among matured bonds
    (one request per bond) runs in parallel."""
    todo = [it for it in items if not it["code"] and it["name"]
            and not (skip_before and it["matdate"] and it["matdate"] < skip_before)]
    if not todo:
        return []
    try:
        bonds = table(get_json("/engines/stock/markets/bonds/securities.json", {
            "iss.only": "securities",
            "securities.columns": "SECID,ISIN,SHORTNAME,SECNAME,LATNAME,MATDATE"}), "securities")
    except Exception as exc:
        return [f"{it['name']}: could not load the bond list ({exc})" for it in todo]
    by_matdate = defaultdict(list)
    for b in bonds:
        by_matdate[parse_date(b.get("matdate"))].append(b)

    def find(it: dict) -> dict | None:
        issuer, _, series = it["name"].rpartition(",")
        if not issuer:                       # no comma — can't tell the series apart
            issuer, series = it["name"], ""
        words = issuer_words(issuer)
        variants = series_variants(series) if series.strip() else set()

        found = None
        if it["matdate"]:                    # first — bonds with the same maturity date
            found = pick(by_matdate[it["matdate"]], words, variants, need_series=False)
        if not found and variants:
            found = pick(bonds, words, variants, need_series=True, matdate=it["matdate"])
        if not found and variants and words:  # among untraded and matured bonds
            try:
                rows = table(get_json("/securities.json", {
                    "q": max(words, key=len), "iss.only": "securities", "limit": 100,
                    "group_by": "group", "group_by_filter": "stock_bonds"}), "securities")
            except Exception:
                rows = []
            found = pick([r for r in rows if "bond" in str(r.get("group") or "bond")],
                         words, variants, need_series=True)
        return found

    problems = []
    for it, found in zip(todo, pmap(find, todo), strict=True):
        if found:
            it["code"] = (found.get("isin") or found.get("secid") or "").upper()
            moex_name = found.get("secname") or found.get("name") or found.get("shortname")
            log(f"  {it['name']}  →  {it['code']}  ({moex_name})")
        elif it["matdate"] and it["matdate"] < date.today():
            it["code"] = NOT_FOUND           # it won't come back — remember not to search again
            log(f"  {it['name']}  →  not found, but matured on {it['matdate']} — no future events")
        else:
            problems.append(f"{it['name']}: no ISIN found by name — "
                            "add it to the watchlist manually")
    return problems


def reuse_resolved(items: list[dict], previous: list[dict]) -> int:
    """Takes the codes found by name in earlier runs — ``previous`` is the watchlist as saved
    then (a bond is recognized by its name and maturity date). Returns how many bonds got a
    code."""
    known = {(it["name"].lower(), it["matdate"]): it["code"]
             for it in previous if it["code"] and it["name"]}
    n = 0
    for it in items:
        code = known.get((it["name"].lower(), it["matdate"]))
        if not it["code"] and code:
            it["code"] = code
            n += 1
    return n


def format_watchlist(items: list[dict]) -> str:
    """The watchlist with the found ISINs, as the CSV the script saves next to the watchlist."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["code", "name", "matdate", "qty"])
    for it in items:
        w.writerow([it["code"], it["name"], it["matdate"] or "", fmt_qty(it["qty"])])
    return buf.getvalue()


# ───────────────────────── MOEX requests ─────────────────────────

_local = threading.local()   # each thread keeps its own persistent connection to MOEX


class Connection(http.client.HTTPSConnection):
    def __init__(self):
        super().__init__(ISS_HOST, timeout=CONNECT_TIMEOUT)

    def connect(self):
        super().connect()                 # TCP and TLS — with the short timeout,
        self.sock.settimeout(TIMEOUT)     # waiting for responses — with the normal one


def fetch(url: str) -> bytes:
    """GET over the thread's persistent connection: a new HTTPS connection to MOEX
    costs ~0.5 s, a request over an open one only the response time."""
    for _ in range(2):
        conn = getattr(_local, "conn", None)
        fresh = conn is None
        if fresh:
            conn = _local.conn = Connection()
        try:
            conn.request("GET", url, headers=HEADERS)
            resp = conn.getresponse()
            body = resp.read()
            break
        except (OSError, http.client.HTTPException):
            conn.close()
            _local.conn = None
            # but MOEX may have closed an old connection while it was idle — then retry on a new one
            if fresh:
                raise
    if resp.status != 200:
        raise HTTPError(f"https://{ISS_HOST}{url}", resp.status, resp.reason, resp.headers, None)
    return gzip.decompress(body) if resp.getheader("Content-Encoding") == "gzip" else body


def connect() -> None:
    """Opens the thread's connection in advance, while the main thread waits for MOEX."""
    if getattr(_local, "conn", None) is None:
        _local.conn = Connection()
        try:
            _local.conn.connect()
        except OSError:
            pass            # didn't work — fetch will open the connection itself


def get_json(path: str, params: dict | None = None, retries: int = 3) -> dict:
    params = {"iss.meta": "off", **(params or {})}
    url = f"{ISS}{path}?{urlencode(params)}"
    for attempt in range(retries):
        try:
            return json.loads(fetch(url))
        except (OSError, http.client.HTTPException, ValueError):
            if attempt == retries - 1:
                raise
            time.sleep(1.5 * (attempt + 1))
    return {}


def table(data: dict, block: str) -> list[dict]:
    """Turns an ISS response block ({columns, data}) into a list of dicts."""
    b = data.get(block) or {}
    cols = [c.lower() for c in b.get("columns", [])]
    return [dict(zip(cols, row, strict=False)) for row in b.get("data", [])]


def cursor_total(data: dict, block: str) -> int | None:
    cur = table(data, f"{block}.cursor")
    return int(cur[0]["total"]) if cur and cur[0].get("total") is not None else None


def find_security(code: str) -> dict | None:
    """Finds a security by ISIN or MOEX code."""
    rows = table(get_json("/securities.json", {"q": code, "iss.only": "securities"}), "securities")
    matches = [r for r in rows
               if code in (str(r.get("secid") or "").upper(), str(r.get("isin") or "").upper())]
    if not matches:
        return None
    # bonds first, then the ones currently traded
    matches.sort(key=lambda r: ("bond" not in str(r.get("group") or ""), not r.get("is_traded")))
    return matches[0]


def describe(secid: str) -> dict:
    """Security card: name, maturity date, face value, etc."""
    rows = table(get_json(f"/securities/{secid}.json", {"iss.only": "description"}), "description")
    return {str(r.get("name") or "").upper(): r.get("value") for r in rows}


def describe_many(codes: list[str]) -> dict[str, dict]:
    """Cards of traded bonds, 50 per request instead of one request each:
    {code: card as from describe}. Matured and untraded bonds aren't here —
    events_for fetches their cards one by one."""
    cards = {}
    for i in range(0, len(codes), CARDS_CHUNK):
        chunk = codes[i:i + CARDS_CHUNK]
        try:
            rows = table(get_json("/engines/stock/markets/bonds/securities.json", {
                "iss.only": "securities", "securities": ",".join(chunk),
                "securities.columns": "SECID,ISIN,SHORTNAME,MATDATE,FACEVALUE,FACEUNIT"}),
                "securities")
        except Exception:
            continue
        for r in rows:
            if r.get("secid") in chunk:
                cards[r["secid"]] = {k.upper(): v for k, v in r.items()}
    return cards


def bondization(secid: str) -> dict[str, list[dict]]:
    """Coupons, amortizations and offers of a bond (all pages)."""
    blocks = ("coupons", "amortizations", "offers")
    path = f"/securities/{secid}/bondization.json"
    only = ",".join(blocks + tuple(f"{b}.cursor" for b in blocks))
    # lang=en — offer types in English; bond names come from the watchlist or the (Russian) cards
    first = get_json(path, {"iss.only": only, "limit": PAGE, "lang": "en"})

    result = {}
    for b in blocks:
        rows = table(first, b)
        total = cursor_total(first, b)
        last_len = len(rows)
        for _ in range(100):  # guard against an endless loop
            more = len(rows) < total if total is not None else last_len == PAGE
            if not more:
                break
            page_data = get_json(path, {"iss.only": f"{b},{b}.cursor", "limit": PAGE,
                                        "start": len(rows), "lang": "en"})
            page = table(page_data, b)
            if not page:
                break
            rows += page
            last_len = len(page)
        result[b] = rows
    return result


# ───────────────────────── events ─────────────────────────

def events_for(code: str, qty: float | None, expected_matdate: date | None = None,
               card: dict | None = None, name: str = "") -> list[dict]:
    # Try the code directly first: for corporate bonds the ISIN is the MOEX code,
    # OFZ codes look like SU26238RMFS4. The search is needed only if MOEX doesn't know
    # the code (e.g. an OFZ given by its ISIN RU000A1038V6) — it costs an extra request.
    sec: dict = {}
    desc = card or describe(code)
    secid = desc.get("SECID")
    if not secid:
        sec = find_security(code) or {}
        if not sec:
            raise LookupError("not found on MOEX")
        secid = sec["secid"]
        desc = describe(secid)
    matdate = parse_date(desc.get("MATDATE"))
    if expected_matdate and matdate and matdate != expected_matdate:
        raise LookupError(f"MOEX maturity date is {matdate}, but the watchlist says "
                          f"{expected_matdate} — looks like a different bond; check ISIN {code}")
    bz = bondization(secid)

    name = name or desc.get("SHORTNAME") or sec.get("shortname") or secid
    isin = desc.get("ISIN") or sec.get("isin") or ""
    events: list[dict] = []

    def add(day, kind, amount=None, cur="", rate=None, note="", remind=None):
        if day is None:
            return
        events.append({
            "date": day, "kind": kind, "name": name, "isin": isin, "secid": secid,
            "amount": amount, "cur": cur, "rate": rate, "qty": qty,
            "total": amount * qty if amount is not None and qty else None,
            "note": note, "remind": remind,
        })

    # Coupons
    for r in bz["coupons"]:
        amount = to_number(r.get("value")) or None     # 0 = not announced yet
        rate = to_number(r.get("valueprc")) or None
        notes = []
        rec = parse_date(r.get("recorddate"))
        if rec:
            notes.append(f"record date {rec}")
        if amount is None:
            notes.append("coupon amount not announced yet")
        add(parse_date(r.get("coupondate")), COUPON, amount, currency_of(r), rate, "; ".join(notes))

    # Amortizations and redemption (the last principal payment is the redemption)
    amort = sorted((r for r in bz["amortizations"] if parse_date(r.get("amortdate"))),
                   key=lambda r: parse_date(r.get("amortdate")))
    for i, r in enumerate(amort):
        day = parse_date(r.get("amortdate"))
        final = (i == len(amort) - 1 or day == matdate
                 or str(r.get("data_source") or "").lower() == "maturity")
        pct = to_number(r.get("valueprc"))
        add(day, REDEMPTION if final else AMORTIZATION,
            to_number(r.get("value")) or None, currency_of(r), None,
            f"{pct:g}% of face value" if pct else "")
    if not amort and matdate:
        add(matdate, REDEMPTION, to_number(desc.get("FACEVALUE")),
            CURRENCY.get(str(desc.get("FACEUNIT") or "").upper(), desc.get("FACEUNIT") or ""))

    # Offers
    for r in bz["offers"]:
        kind = str(r.get("offertype") or "Offer").strip()
        price = to_number(r.get("price"))
        face = to_number(r.get("facevalue"))
        details = kind + (f", price {price:g}% of face value" if price else "")
        start, end = parse_date(r.get("offerdatestart")), parse_date(r.get("offerdateend"))
        if start and end and start != end:
            add(start, OFFER_OPENS, note=f"{details}; applications until {end} inclusive")
            add(end, OFFER_LAST_DAY,
                note=f"{details}; your broker's deadline is usually earlier — check",
                remind=REMIND_WINDOW)
        elif start or end:
            add(start or end, OFFER_DAY, note=details, remind=REMIND_WINDOW)
        amount = price * face / 100 if price and face else None
        if start or end:
            add(parse_date(r.get("offerdate")), OFFER_BUYBACK, amount, currency_of(r), None,
                details)
        else:
            # MOEX publishes the application dates shortly before the offer — remind early
            add(parse_date(r.get("offerdate")), OFFER_BUYBACK, amount, currency_of(r), None,
                f"{details}; application dates not announced yet — applications close "
                f"a few days before the buyback, watch the issuer's announcements",
                remind=REMIND_NO_WINDOW)

    return events


@dataclass
class CalendarRun:
    """What one run produced: the events by date, and what the script would print to stderr."""

    events: list[dict]
    problems: list[str] = field(default_factory=list)  # bonds that could not be processed
    notes: list[str] = field(default_factory=list)  # skipped bonds and the like


def build_calendar(items: list[dict], date_from: date | None, date_to: date | None, *,
                   workers: int = 16, log: Callable[[str], None] = lambda msg: None) -> CalendarRun:
    """The script's main(): events for the watchlist between date_from and date_to (None = no
    limit on that side). Bonds without a code are looked up by name for this run only — saving
    the found codes is the caller's business."""
    problems: list[str] = []
    notes: list[str] = []

    # bonds that already matured (by the watchlist date) won't get into the period — don't spend
    # requests on them
    matured = [it for it in items if date_from and it["matdate"] and it["matdate"] < date_from]
    if matured:
        notes.append(f"Skipping {len(matured)} already matured bonds "
                     "(show the whole calendar to include them).")
    gone = [it for it in items if it["code"] == NOT_FOUND and it not in matured]
    if gone:
        notes.append(f"Skipping {len(gone)} matured bonds that MOEX no longer lists "
                     f'(code "{NOT_FOUND}" in the watchlist).')

    events = []
    # several bonds in parallel: one by one it takes minutes
    workers = max(1, min(workers, len(items) - len(matured) - len(gone)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        # the threads open their connections to MOEX while the main one waits for the bond list
        # and cards
        for _ in range(workers):
            pool.submit(connect)

        if any(not it["code"] and it not in matured for it in items):
            log("Looking up ISINs by name (check that everything matched correctly):")
            problems += resolve_names(items, log, skip_before=date_from, pmap=pool.map)

        todo = [it for it in items if it["code"] not in ("", NOT_FOUND) and it not in matured]
        cards = describe_many([it["code"] for it in todo])

        def one(it):
            try:
                evs = events_for(it["code"], it["qty"], it["matdate"], cards.get(it["code"]),
                                 it["name"])
                return evs, None
            except Exception as exc:  # one bad bond must not break the whole list
                return [], f"{it['name'] or it['code']}: {exc}"

        for i, (evs, err) in enumerate(pool.map(one, todo), 1):
            log(f"[{i}/{len(todo)}] {todo[i - 1]['code']}" + (" — error" if err else ""))
            events += evs
            if err:
                problems.append(err)

    if date_from:
        events = [e for e in events if e["date"] >= date_from]
    if date_to:
        events = [e for e in events if e["date"] <= date_to]
    events.sort(key=lambda e: (e["date"], e["name"], e["kind"]))
    return CalendarRun(events, problems, notes)


# ───────────────────────── output ─────────────────────────

def fmt(x: float | None, decimal_comma: bool = False) -> str:
    if x is None:
        return ""
    s = f"{x:.2f}" if abs(x - round(x, 2)) < 1e-9 else f"{x:.4f}".rstrip("0")
    return s.replace(".", ",") if decimal_comma else s


def fmt_qty(q: float | None, decimal_comma: bool = False) -> str:
    if q is None:
        return ""
    s = f"{q:g}"
    return s.replace(".", ",") if decimal_comma else s


def csv_text(events: list[dict], sep: str = ",") -> str:
    """The script's CSV table. Encode it as utf-8-sig, as the script writes it (Excel needs the
    BOM to read the Cyrillic)."""
    dc = sep == ";"   # Excel with a comma decimal separator: ";" delimiter and decimal commas
    buf = io.StringIO()
    w = csv.writer(buf, delimiter=sep)
    w.writerow(COLUMNS)
    for e in events:
        w.writerow([
            e["date"].isoformat(), e["kind"], e["name"], e["isin"], e["secid"],
            fmt(e["amount"], dc), e["cur"], fmt(e["rate"], dc),
            fmt_qty(e["qty"], dc),
            fmt(e["total"], dc), e["note"],
        ])
    return buf.getvalue()


def ics_text(s: str) -> str:
    return s.replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def ics_fold(line: str) -> str:
    """.ics lines are at most 75 bytes long (the standard requires it)."""
    parts, cur = [], b""
    for ch in line:
        b = ch.encode("utf-8")
        if len(cur) + len(b) > 74:
            parts.append(cur.decode("utf-8"))
            cur = b" " + b
        else:
            cur += b
    parts.append(cur.decode("utf-8"))
    return "\r\n".join(parts)


def ics_calendar(events: list[dict], remind_days: int = 3) -> str:
    """The script's .ics file. Event IDs are the script's too, so a calendar that imported the
    script's file doesn't duplicate events."""
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//bond-calendar//EN",
           "CALSCALE:GREGORIAN", "X-WR-CALNAME:Bonds"]
    for e in events:
        uid = hashlib.sha1(f"{e['date']}|{e['kind']}|{e['secid']}".encode()).hexdigest()
        summary = f"{e['kind']} — {e['name']}"
        if e["amount"] is not None:
            summary += f" — {fmt(e['amount'])} {e['cur']}"
        desc = [f"ISIN: {e['isin']}"]
        if e["total"] is not None:
            desc.append(f"Total for {fmt_qty(e['qty'])} pcs: {fmt(e['total'])} {e['cur']}")
        if e["note"]:
            desc.append(e["note"])
        out += ["BEGIN:VEVENT", f"UID:{uid}@bond-calendar", f"DTSTAMP:{stamp}",
                f"DTSTART;VALUE=DATE:{e['date']:%Y%m%d}",
                f"DTEND;VALUE=DATE:{e['date'] + timedelta(days=1):%Y%m%d}",
                f"SUMMARY:{ics_text(summary)}", f"DESCRIPTION:{ics_text(chr(10).join(desc))}",
                "TRANSP:TRANSPARENT"]
        days = remind_days if e.get("remind") == REMIND_WINDOW else e.get("remind")
        if days and days > 0:
            out += ["BEGIN:VALARM", "ACTION:DISPLAY", f"DESCRIPTION:{ics_text(summary)}",
                    f"TRIGGER:-P{days}D", "END:VALARM"]
        out.append("END:VEVENT")
    out.append("END:VCALENDAR")
    return "\r\n".join(ics_fold(line) for line in out) + "\r\n"


def monthly_cash(events: list[dict]) -> list[tuple[str, str, float]]:
    """Expected payments by month (if the watchlist has quantities): coupons + principal, before
    tax, as (YYYY-MM, currency, total) — the summary the script prints under the calendar."""
    cash: dict[tuple[str, str], float] = defaultdict(float)
    for e in events:
        if e["total"] is not None and e["kind"] in (COUPON, AMORTIZATION, REDEMPTION):
            cash[(f"{e['date']:%Y-%m}", e["cur"])] += e["total"]
    return [(m, cur, total) for (m, cur), total in sorted(cash.items())]
