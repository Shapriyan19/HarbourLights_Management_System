from __future__ import annotations

import json
import os
import re
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv

from .cleaning import money

load_dotenv(Path(__file__).resolve().parent.parent / ".env")

# "gemini-2.5-flash" is no longer served to new API keys (404 NOT_FOUND).
# "gemini-flash-latest" is the maintained alias that always points at the
# current stable Flash model, so drafts & Q&A keep working across model retirements.
DEFAULT_MODEL = "gemini-flash-latest"

# Last error from a live call, so the UI can explain *why* AI fell back to
# templates instead of silently pretending everything is fine.
_LAST_ERROR: str | None = None


def last_error() -> str | None:
    return _LAST_ERROR


def _model_name() -> str:
    return os.environ.get("GEMINI_MODEL", DEFAULT_MODEL)


def _client():
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        return None
    try:
        from google import genai
        return genai.Client(api_key=key)
    except Exception:
        return None


def ai_available() -> bool:
    return _client() is not None


def _generate(system: str | None, prompt: str, max_tokens: int) -> str | None:
    """One-shot Gemini call. Returns text, or None on any failure."""
    global _LAST_ERROR
    client = _client()
    if client is None:
        return None
    try:
        from google.genai import types
        cfg = types.GenerateContentConfig(
            system_instruction=system,
            max_output_tokens=max_tokens,
            temperature=0.4,
        )
        resp = client.models.generate_content(
            model=_model_name(), contents=prompt, config=cfg)
        _LAST_ERROR = None
        return (resp.text or "").strip() or None
    except Exception as e:
        _LAST_ERROR = f"{type(e).__name__}: {e}"
        return None

def draft_thank_you(name: str, amount: float, campaign: str,
                    donor_type: str = "", gifts: int = 1) -> str:
    first = (name or "friend").split()[0]
    context = {
        "first_name": first,
        "amount_sgd": None if amount is None else round(float(amount)),
        "campaign": campaign or None,
        "returning_donor": gifts > 1,
    }
    system = (
        "You write short, warm thank-you notes for Harbour Lights, a small "
        "social service charity helping low-income families. Write a 3-4 "
        "sentence email body (no subject, no signature). Warm, specific, not "
        "gushing. Use only the facts given; invent nothing."
    )
    text = _generate(system, json.dumps(context), max_tokens=300)
    return text or _fallback_thank_you(first, amount, campaign, gifts)


def _fallback_thank_you(first, amount, campaign, gifts) -> str:
    amt = money(amount) if amount is not None else "your"
    camp = f" to our {campaign}" if campaign else ""
    again = "once again " if gifts > 1 else ""
    return (
        f"Dear {first},\n\n"
        f"Thank you {again}for your generous gift of {amt}{camp}. Your support "
        f"directly helps low-income families in our community with emergency "
        f"aid, food packs and practical assistance. We are grateful to have you "
        f"alongside us.\n\n"
        f"With warm thanks,\nThe Harbour Lights Team"
    )

def answer_question(question: str, df: pd.DataFrame, donors: pd.DataFrame) -> dict:
    """
    Return {'answer': str, 'table': DataFrame|None}.
    The LLM only picks an intent + parameters; ALL numbers are computed here
    from the clean data, so the figures are always real.
    """
    spec = _interpret(question, df)
    return _execute(spec, df, donors)


INTENTS = """You convert a question into a JSON intent. Return ONLY JSON, no prose.
Choose one intent and fill its parameters:
- {{"intent": "top_donors", "n": <int>}}
- {{"intent": "campaign_total", "campaign": "<name>"}}   # match a campaign below
- {{"intent": "donor_total", "name": "<donor name>"}}
- {{"intent": "total_raised"}}
- {{"intent": "unthanked_count"}}
- {{"intent": "at_risk_count"}}
Campaigns available: {campaigns}
"""


def _interpret(question: str, df: pd.DataFrame) -> dict:
    campaigns = sorted({c for c in df["campaign_canonical"] if c})
    system = INTENTS.format(campaigns=campaigns)
    txt = _generate(system, question, max_tokens=200)
    if txt:
        try:
            txt = txt[txt.find("{"): txt.rfind("}") + 1]
            return json.loads(txt)
        except Exception:
            pass
    return _keyword_intent(question, campaigns)


_GENERIC = {"appeal", "fund", "the", "2025", "2026", "campaign"}


def _keyword_intent(q: str, campaigns: list[str]) -> dict:
    ql = q.lower()
    if "top" in ql or "principal" in ql or "biggest" in ql or "largest" in ql:
        n = next((int(t) for t in ql.split() if t.isdigit()), 10)
        return {"intent": "top_donors", "n": n}
    # match the campaign whose *distinctive* words best appear in the question
    best, score = None, 0
    for c in campaigns:
        toks = [t for t in re.split(r"[\s-]+", c.lower()) if t not in _GENERIC]
        hits = sum(1 for t in toks if t in ql)
        if hits > score:
            best, score = c, hits
    if best:
        return {"intent": "campaign_total", "campaign": best}
    if "thank" in ql:
        return {"intent": "unthanked_count"}
    if "lapse" in ql or "risk" in ql or "inactive" in ql:
        return {"intent": "at_risk_count"}
    if "total" in ql or "raised" in ql or "how much" in ql:
        return {"intent": "total_raised"}
    return {"intent": "total_raised"}


def _execute(spec: dict, df: pd.DataFrame, donors: pd.DataFrame) -> dict:
    from .cleaning import at_risk, unthanked
    intent = spec.get("intent")
    sgd = df[df["currency"].isin(["SGD", ""])]

    if intent == "top_donors":
        n = int(spec.get("n", 10) or 10)
        t = donors.head(n)[["name", "donor_type", "gifts", "lifetime_sgd"]]
        return {"answer": f"Top {n} donors by lifetime giving (SGD gifts):",
                "table": t.reset_index(drop=True)}

    if intent == "campaign_total":
        from .cleaning import canonical_campaign
        camp = canonical_campaign(spec.get("campaign", ""))
        rows = sgd[sgd["campaign_canonical"] == camp]
        total = rows["amount"].sum()
        return {"answer": f"{camp or 'That campaign'} raised {money(total)} "
                          f"across {len(rows)} gifts.",
                "table": rows[["full_name", "amount", "date"]].reset_index(drop=True)}

    if intent == "donor_total":
        nm = spec.get("name", "").lower()
        rows = df[df["full_name"].str.lower().str.contains(nm, na=False)]
        total = rows[rows["currency"].isin(["SGD", ""])]["amount"].sum()
        return {"answer": f"{spec.get('name','')} has given {money(total)} "
                          f"across {len(rows)} gifts.",
                "table": rows[["full_name", "amount", "campaign", "date"]].reset_index(drop=True)}

    if intent == "unthanked_count":
        u = unthanked(df)
        return {"answer": f"{len(u)} gifts have not been thanked yet.",
                "table": u[["full_name", "amount", "campaign", "date"]].reset_index(drop=True)}

    if intent == "at_risk_count":
        r = at_risk(donors)
        return {"answer": f"{len(r)} monthly donors look at risk of lapsing.",
                "table": r[["name", "days_since", "lifetime_sgd"]].reset_index(drop=True)}

    total = sgd["amount"].sum()
    return {"answer": f"Total raised (SGD gifts): {money(total)}.", "table": None}
