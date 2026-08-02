from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import pandas as pd
from rapidfuzz import fuzz

EXCEL_EPOCH = date(1899, 12, 30)
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

# A monthly donor silent for longer than this many days is "at risk".
LAPSE_DAYS = 45

RAW_COLUMNS = [
    "donor_id", "full_name", "email", "phone", "amount", "currency",
    "donation_date", "campaign", "donor_type", "thanked", "notes",
]


def parse_date(value) -> date | None:
    """Handle Excel serials, ISO, dotted, and slash date formats."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (datetime, date)):
        return value.date() if isinstance(value, datetime) else value
    # Excel serial number (stored as a plain number, not date-formatted)
    if isinstance(value, (int, float)):
        try:
            return EXCEL_EPOCH + timedelta(days=int(value))
        except (ValueError, OverflowError):
            return None
    s = str(value).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.0)?", s):  # serial that arrived as text
        return EXCEL_EPOCH + timedelta(days=int(float(s)))
    for fmt in ("%Y.%m.%d", "%Y-%m-%d", "%d/%m/%Y", "%m/%d/%Y",
                "%d-%m-%Y", "%d %b %Y", "%d %B %Y", "%b %d, %Y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            continue
    return None


def parse_amount(value) -> float | None:
    """Strip $ and commas; return a clean float or None."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = re.sub(r"[,$\s]", "", str(value))
    try:
        return float(s)
    except ValueError:
        return None


def clean_str(value) -> str:
    """Coerce any cell (incl. pandas NaN / None) to a trimmed string, never 'nan'."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    return str(value).strip()


def normalise_currency(value) -> str:
    return clean_str(value).upper()


def normalise_name(value) -> str:
    return re.sub(r"\s+", " ", clean_str(value))


def name_key(name: str) -> str:
    """Order-independent, case-insensitive key so 'Tan Wei Ming' == 'Wei Ming Tan'."""
    tokens = re.sub(r"[^a-z\s]", "", name.lower()).split()
    return " ".join(sorted(tokens))


def canonical_campaign(value) -> str:
    raw = clean_str(value)
    if not raw:
        return ""
    k = raw.lower()
    if "building" in k:
        return "Building Fund"
    if "ramadan" in k:
        return "Ramadan Appeal 2026"
    if "mid" in k and "year" in k:
        return "Mid-Year Appeal"
    if re.search(r"year[\s-]?end", k) or re.fullmatch(r"ye\s*\d{2,4}", k):
        return "Year-End 2025"
    return raw


def normalise_phone(value) -> str:
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return ""
    if isinstance(value, float):
        value = f"{value:.0f}"
    digits = re.sub(r"\D", "", str(value))
    return digits[-8:] if len(digits) >= 8 else digits  # local SG part


def valid_email(email: str) -> bool:
    return bool(EMAIL_RE.fullmatch(email.strip())) if email else False

def load_clean(path: str) -> pd.DataFrame:
    raw = pd.read_excel(path, dtype=object)
    raw.columns = [str(c).strip() for c in raw.columns]

    rows = []
    for i, r in raw.iterrows():
        rows.append({
            "row": int(i) + 2,  # 1-based + header, matches spreadsheet row
            "donor_id": normalise_name(r.get("donor_id")),
            "full_name": normalise_name(r.get("full_name")),
            "email": clean_str(r.get("email")).lower(),
            "phone": normalise_phone(r.get("phone")),
            "amount": parse_amount(r.get("amount")),
            "currency": normalise_currency(r.get("currency")),
            "date": parse_date(r.get("donation_date")),
            "campaign": normalise_name(r.get("campaign")),
            "donor_type": normalise_name(r.get("donor_type")).lower(),
            "thanked_date": parse_date(r.get("thanked")),
            "notes": normalise_name(r.get("notes")),
        })
    df = pd.DataFrame(rows)
    df["thanked"] = df["thanked_date"].notna()
    df["name_key"] = df["full_name"].map(name_key)
    df["campaign_canonical"] = df["campaign"].map(canonical_campaign)
    df["canonical_id"] = assign_canonical_ids(df)
    return df


def assign_canonical_ids(df: pd.DataFrame) -> pd.Series:
    parent = {i: i for i in df.index}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    def union(a, b):
        parent[find(a)] = find(b)

    # exact email
    for _, grp in df[df["email"] != ""].groupby("email"):
        idx = grp.index.tolist()
        for j in idx[1:]:
            union(idx[0], j)
    # exact phone
    for _, grp in df[df["phone"] != ""].groupby("phone"):
        idx = grp.index.tolist()
        for j in idx[1:]:
            union(idx[0], j)
    # fuzzy name (only bridge when no email conflict)
    idxs = df.index.tolist()
    for a in range(len(idxs)):
        for b in range(a + 1, len(idxs)):
            i, j = idxs[a], idxs[b]
            ni, nj = df.at[i, "name_key"], df.at[j, "name_key"]
            if not ni or not nj:
                continue
            if fuzz.token_sort_ratio(ni, nj) >= 90:
                ei, ej = df.at[i, "email"], df.at[j, "email"]
                if not ei or not ej or ei == ej:
                    union(i, j)

    # label each cluster with the lowest donor_id present (stable, human-friendly)
    roots = {}
    for i in df.index:
        roots.setdefault(find(i), []).append(i)
    labels = {}
    for members in roots.values():
        ids = sorted({df.at[m, "donor_id"] for m in members if df.at[m, "donor_id"]})
        label = ids[0] if ids else f"row{df.at[members[0], 'row']}"
        for m in members:
            labels[m] = label
    return pd.Series(labels)


@dataclass
class Issue:
    kind: str          # duplicate | missing | invalid | currency | anomaly
    severity: str      # high | medium | low
    title: str
    detail: str
    rows: list = field(default_factory=list)      # spreadsheet row numbers
    donors: list = field(default_factory=list)     # donor ids involved


def detect_issues(df: pd.DataFrame) -> list[Issue]:
    issues: list[Issue] = []

    # --- probable duplicates: one canonical id spanning >1 donor_id ---------
    for canon, grp in df.groupby("canonical_id"):
        ids = sorted({x for x in grp["donor_id"] if x})
        if len(ids) > 1:
            emails = {e for e in grp["email"] if e}
            reason = ("identical email + matching name"
                      if len(emails) == 1 else "matching name / phone")
            issues.append(Issue(
                kind="duplicate", severity="high",
                title=f"Probable duplicate: {' / '.join(sorted(set(grp['full_name'])))}",
                detail=f"Donor IDs {', '.join(ids)} look like the same person "
                       f"({reason}). {len(grp)} gifts totalling "
                       f"{money(grp['amount'].sum())}.",
                rows=sorted(grp["row"]), donors=ids,
            ))

    # --- missing critical fields -------------------------------------------
    for _, r in df.iterrows():
        missing = []
        if r["amount"] is None:
            missing.append("amount")
        if r["date"] is None:
            missing.append("date")
        if not r["donor_id"]:
            missing.append("donor ID")
        if missing:
            issues.append(Issue(
                kind="missing", severity="high" if "amount" in missing else "medium",
                title=f"Missing {', '.join(missing)}: {r['full_name'] or '(no name)'}",
                detail=f"Row {r['row']} · {r['campaign'] or 'no campaign'}",
                rows=[r["row"]], donors=[r["donor_id"]] if r["donor_id"] else [],
            ))

    # --- invalid email ------------------------------------------------------
    for _, r in df.iterrows():
        if r["email"] and not valid_email(r["email"]):
            issues.append(Issue(
                kind="invalid", severity="medium",
                title=f"Invalid email: {r['full_name']}",
                detail=f"'{r['email']}' is not a valid address (row {r['row']}).",
                rows=[r["row"]], donors=[r["donor_id"]],
            ))

    # --- non-base currency needs FX conversion ------------------------------
    for _, r in df.iterrows():
        if r["currency"] and r["currency"] != "SGD":
            issues.append(Issue(
                kind="currency", severity="medium",
                title=f"Non-SGD gift: {r['currency']} from {r['full_name']}",
                detail=f"{r['currency']} {r['amount']:.0f} (row {r['row']}) — "
                       f"needs conversion before it counts toward SGD totals.",
                rows=[r["row"]], donors=[r["donor_id"]],
            ))
        elif not r["currency"] and r["amount"] is not None:
            issues.append(Issue(
                kind="missing", severity="low",
                title=f"Missing currency: {r['full_name']}",
                detail=f"Row {r['row']} has an amount but no currency.",
                rows=[r["row"]], donors=[r["donor_id"]],
            ))

    # --- anomalous amount within donor_type --------------------------------
    for dtype, grp in df[df["amount"].notna()].groupby("donor_type"):
        amts = grp["amount"].astype(float)
        if len(amts) < 5:
            continue
        mean, std = amts.mean(), amts.std()
        if std == 0:
            continue
        for _, r in grp.iterrows():
            if (r["amount"] - mean) / std > 3:
                issues.append(Issue(
                    kind="anomaly", severity="low",
                    title=f"Unusually large {dtype} gift: {r['full_name']}",
                    detail=f"{money(r['amount'])} vs typical {money(mean)} "
                           f"for {dtype} donors (row {r['row']}). Worth a sanity check.",
                    rows=[r["row"]], donors=[r["donor_id"]],
                ))

    # --- campaign spelling variants consolidated ---------------------------
    for canon, grp in df[df["campaign_canonical"] != ""].groupby("campaign_canonical"):
        variants = sorted({c for c in grp["campaign"] if c and c != canon})
        if variants:
            issues.append(Issue(
                kind="campaign", severity="low",
                title=f"Campaign name variants merged into '{canon}'",
                detail=f"Also written as: {', '.join(variants)}. "
                       f"Totals now combine all of these ({money(grp['amount'].sum())}).",
                rows=sorted(grp["row"]), donors=[],
            ))

    order = {"high": 0, "medium": 1, "low": 2}
    issues.sort(key=lambda x: (order[x.severity], x.kind))
    return issues

def reference_today(df: pd.DataFrame) -> date:
    latest = max((d for d in df["date"] if d), default=date.today())
    return max(date.today(), latest)


def donor_summary(df: pd.DataFrame) -> pd.DataFrame:
    """One row per real donor (canonical), with rollups for the dashboard."""
    today = reference_today(df)
    out = []
    for canon, grp in df.groupby("canonical_id"):
        gifts = grp[grp["amount"].notna()]
        last = max((d for d in grp["date"] if d), default=None)
        types = [t for t in grp["donor_type"] if t]
        is_monthly = "monthly" in types
        out.append({
            "canonical_id": canon,
            "name": grp.sort_values("row")["full_name"].iloc[-1],
            "email": next((e for e in grp["email"] if e), ""),
            "donor_type": "monthly" if is_monthly else (types[0] if types else ""),
            "gifts": len(grp),
            "lifetime_sgd": gifts[gifts["currency"].isin(["SGD", ""])]["amount"].sum(),
            "last_gift": last,
            "days_since": (today - last).days if last else None,
            "unthanked": int((~grp["thanked"]).sum()),
            "merged_ids": sorted({x for x in grp["donor_id"] if x}),
        })
    return pd.DataFrame(out).sort_values("lifetime_sgd", ascending=False)


def at_risk(donors: pd.DataFrame) -> pd.DataFrame:
    m = donors[(donors["donor_type"] == "monthly")
               & (donors["days_since"].notna())
               & (donors["days_since"] > LAPSE_DAYS)]
    return m.sort_values("lifetime_sgd", ascending=False)


def unthanked(df: pd.DataFrame) -> pd.DataFrame:
    u = df[(~df["thanked"]) & (df["amount"].notna())].copy()
    return u.sort_values("date", ascending=False)


def money(x) -> str:
    try:
        return f"S${float(x):,.0f}"
    except (TypeError, ValueError):
        return "—"
