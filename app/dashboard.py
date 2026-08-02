"""
Harbour Lights donor management dashboard.

Run:  streamlit run app/dashboard.py
"""
from __future__ import annotations

import json
import os
import sys
from io import BytesIO
from pathlib import Path

# Allow `streamlit run app/dashboard.py` from anywhere: put the project root
# (this file's parent's parent) on the path so `import app.*` resolves.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pandas as pd
import streamlit as st

from app.cleaning import (LAPSE_DAYS, RAW_COLUMNS, at_risk, detect_issues,
                          donor_summary, load_clean, money, reference_today,
                          unthanked)
from app.ai import ai_available, answer_question, draft_thank_you, last_error

DATA_DIR = Path(__file__).resolve().parent.parent / "data"
DEFAULT_FILE = DATA_DIR / "donations.xlsx"
ACTIONS_FILE = DATA_DIR / "actions.json"

st.set_page_config(page_title="Harbour Lights · Donors", page_icon="⚓", layout="wide")


# --------------------------------------------------------------------------- #
# Persistence of Jonathan's actions (marked-thanked, dismissed issues)
# --------------------------------------------------------------------------- #
def load_actions() -> dict:
    if ACTIONS_FILE.exists():
        return json.loads(ACTIONS_FILE.read_text())
    return {"thanked_rows": [], "dismissed": []}


def save_actions(a: dict) -> None:
    ACTIONS_FILE.write_text(json.dumps(a, indent=2))


# Bump when the cleaned-data schema changes, so the cache invalidates on rerun
# instead of serving a stale table (e.g. after a new derived column is added).
SCHEMA_VERSION = 2


@st.cache_data(show_spinner=False)
def get_data(path: str, _mtime: float, _schema: int):
    df = load_clean(path)
    return df


@st.cache_data(show_spinner=False)
def get_raw(path: str, _mtime: float):
    """The spreadsheet exactly as stored, for editing and round-tripping.

    Unlike get_data (which derives analytics columns), this keeps the original
    RAW_COLUMNS so edits save back in the same shape the file arrived in.
    """
    raw = pd.read_excel(path, dtype=object)
    raw.columns = [str(c).strip() for c in raw.columns]
    # Guarantee every expected column exists and is ordered predictably, so the
    # editor and export stay stable even if an imported file omits a column.
    for col in RAW_COLUMNS:
        if col not in raw.columns:
            raw[col] = pd.NA
    return raw[RAW_COLUMNS]


def to_excel_bytes(df: pd.DataFrame) -> bytes:
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as writer:
        df.to_excel(writer, index=False, sheet_name="donations")
    return buf.getvalue()


def load_frames(path: str):
    df = get_data(path, os.path.getmtime(path), SCHEMA_VERSION)
    actions = st.session_state.actions
    # apply "marked thanked" overrides
    df = df.copy()
    df.loc[df["row"].isin(actions["thanked_rows"]), "thanked"] = True
    donors = donor_summary(df)
    issues = [i for i in detect_issues(df)
              if issue_key(i) not in actions["dismissed"]]
    return df, donors, issues


def issue_key(i) -> str:
    return f"{i.kind}:{'-'.join(map(str, i.rows))}"


# --------------------------------------------------------------------------- #
# Sidebar
# --------------------------------------------------------------------------- #
if "actions" not in st.session_state:
    st.session_state.actions = load_actions()

st.sidebar.title("⚓ Harbour Lights")
st.sidebar.caption("Donor management")

uploaded = st.sidebar.file_uploader("Import spreadsheet (.xlsx)", type=["xlsx"])
if uploaded:
    tmp = DATA_DIR / "_uploaded.xlsx"
    tmp.write_bytes(uploaded.getbuffer())
    active_path = str(tmp)
else:
    active_path = str(DEFAULT_FILE)

page = st.sidebar.radio(
    "Go to",
    ["Overview", "Edit data", "Data Health", "At Risk",
     "Acknowledgements", "Ask the data"],
)

st.sidebar.divider()
st.sidebar.caption(
    ("🟢 AI connected (Gemini)" if ai_available()
     else "⚪ AI off — add GEMINI_API_KEY to .env for live drafts & Q&A "
          "(templates used meanwhile)")
)

df, donors, issues = load_frames(active_path)
today = reference_today(df)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #
def page_overview():
    st.title("Overview")
    st.caption(f"As of {today:%d %b %Y} · {len(df)} gifts · "
               f"{donors['canonical_id'].nunique()} donors")

    risk = at_risk(donors)
    unt = unthanked(df)
    total = df[df["currency"].isin(["SGD", ""])]["amount"].sum()

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Total raised (SGD)", money(total))
    c2.metric("Donors", donors["canonical_id"].nunique())
    c3.metric("At risk of lapsing", len(risk))
    c4.metric("Not yet thanked", len(unt))

    st.subheader("⚠ Needs your attention")
    st.write(f"- **{len(unt)}** gifts given but not thanked")
    st.write(f"- **{len(risk)}** monthly donors may have lapsed "
             f"(silent > {LAPSE_DAYS} days)")
    st.write(f"- **{len(issues)}** records need cleaning "
             f"(duplicates, missing fields, anomalies)")

    st.subheader("Raised by campaign")
    camp = (df[df["currency"].isin(["SGD", ""])]
            .assign(campaign=lambda d: d["campaign_canonical"].replace("", "(none)"))
            .groupby("campaign")["amount"].sum().sort_values(ascending=False))
    st.bar_chart(camp)


def page_data_health():
    st.title("Data Health")
    st.caption(f"{len(issues)} records flagged for review. Nothing changes "
               "automatically — you approve each fix.")

    kinds = ["all", "duplicate", "missing", "invalid", "currency", "campaign", "anomaly"]
    pick = st.segmented_control("Filter", kinds, default="all") or "all"
    shown = [i for i in issues if pick == "all" or i.kind == pick]

    sev_emoji = {"high": "🔴", "medium": "🟠", "low": "🟡"}
    for i in shown:
        with st.container(border=True):
            st.markdown(f"{sev_emoji[i.severity]} **{i.title}**  "
                        f"·  _{i.kind}_")
            st.caption(i.detail)
            cols = st.columns([1, 1, 6])
            if i.kind == "duplicate":
                if cols[0].button("Merge", key=f"m{issue_key(i)}",
                                  help="Give these rows one donor ID and save it "
                                       "to the spreadsheet (each gift is kept)."):
                    _merge_duplicate(i)
                    st.rerun()
                cols[1].button("Not the same", key=f"n{issue_key(i)}",
                               on_click=_dismiss, args=(i,))
            else:
                cols[0].button("Mark resolved", key=f"r{issue_key(i)}",
                               on_click=_dismiss, args=(i,))
    if not shown:
        st.success("No issues in this category. 🎉")


def _dismiss(i):
    st.session_state.actions["dismissed"].append(issue_key(i))
    save_actions(st.session_state.actions)


def _merge_duplicate(i) -> None:
    """Consolidate a duplicate cluster onto one donor_id, in the spreadsheet.

    Keeps every gift as its own row (donation history is preserved) — it only
    rewrites the donor_id of the flagged rows to the surviving id, then saves.
    Because duplicates are detected as one canonical donor spanning >1 donor_id,
    unifying the id makes the flag disappear on its own; no dismissal needed.
    """
    raw = get_raw(active_path, os.path.getmtime(active_path)).copy()
    ids = sorted(d for d in i.donors if d)
    surviving = ids[0] if ids else ""
    if not surviving:
        return
    col = raw.columns.get_loc("donor_id")
    for row_num in i.rows:
        idx = row_num - 2  # spreadsheet row -> 0-based DataFrame index
        if 0 <= idx < len(raw):
            raw.iat[idx, col] = surviving
    raw.to_excel(active_path, index=False, sheet_name="donations")
    get_raw.clear()
    get_data.clear()
    st.session_state.pop("editor", None)
    st.toast(f"Merged onto donor {surviving} — updated the spreadsheet", icon="🔗")


def page_at_risk():
    st.title("At Risk of Lapsing")
    risk = at_risk(donors)
    st.caption(f"{len(risk)} monthly donors silent for more than "
               f"{LAPSE_DAYS} days, biggest givers first.")
    if risk.empty:
        st.success("No monthly donors are currently at risk. 🎉")
        return
    view = risk.assign(
        last_gift=lambda d: d["last_gift"].map(lambda x: f"{x:%d %b %Y}" if x else "—"),
        silent=lambda d: d["days_since"].map(lambda x: f"{int(x)} days"),
        lifetime=lambda d: d["lifetime_sgd"].map(money),
    )[["name", "donor_type", "last_gift", "silent", "lifetime", "gifts"]]
    st.dataframe(view, hide_index=True, width='stretch')
    st.caption("Suggested action: a quick personal call or note. Draft one on "
               "the Acknowledgements page.")


def page_ack():
    st.title("Acknowledgements")
    unt = unthanked(df)
    st.caption(f"{len(unt)} gifts have been given but not thanked. "
               "Generate a draft, review it, then mark as thanked.")
    if unt.empty:
        st.success("Everyone has been thanked. 🎉")
        return

    for _, r in unt.iterrows():
        with st.container(border=True):
            top = st.columns([3, 2, 2, 2])
            top[0].markdown(f"**{r['full_name'] or '(no name)'}**")
            top[1].write(money(r["amount"]))
            top[2].write(r["campaign"] or "—")
            top[3].write(f"{r['date']:%d %b %Y}" if r["date"] else "—")

            if not r["email"]:
                st.caption("⚠ No email on file — thank by phone/post.")

            b = st.columns([1, 1, 6])
            dkey = f"draft{r['row']}"
            if b[0].button("Draft", key=f"btn{r['row']}"):
                gifts = int(donors.loc[donors["canonical_id"] ==
                                       r["canonical_id"], "gifts"].iloc[0])
                st.session_state[dkey] = draft_thank_you(
                    r["full_name"], r["amount"], r["campaign"],
                    r["donor_type"], gifts)
            if b[1].button("Mark thanked", key=f"thx{r['row']}"):
                st.session_state.actions["thanked_rows"].append(int(r["row"]))
                save_actions(st.session_state.actions)
                st.rerun()
            if dkey in st.session_state:
                st.text_area("Draft (edit before sending)",
                             st.session_state[dkey], height=170,
                             key=f"ta{r['row']}")


def page_ask():
    st.title("Ask the data")
    st.caption("Plain-English questions for the director. The AI only "
               "interprets the question — every number is computed from your "
               "clean data, so figures are always exact.")

    st.write("**Quick reports**")
    q = st.columns(3)
    preset = None
    if q[0].button("Top 10 donors"):
        preset = "Who are the top 10 donors?"
    if q[1].button("Total by campaign"):
        preset = "totals by campaign"
    if q[2].button("Total raised"):
        preset = "How much have we raised in total?"

    question = st.text_input("Ask a question",
                             value=preset or "",
                             placeholder="e.g. How much did the Building Fund raise?")
    if question:
        if "campaign" in question.lower() and "by" in question.lower():
            camp = (df[df["currency"].isin(["SGD", ""])]
                    .assign(campaign=lambda d: d["campaign"].replace("", "(none)"))
                    .groupby("campaign")["amount"].sum()
                    .sort_values(ascending=False).map(money))
            st.write("**Raised by campaign**")
            st.dataframe(camp, width='stretch')
            return
        res = answer_question(question, df, donors)
        st.success(res["answer"])
        if ai_available() and last_error():
            st.caption(f"⚠ AI call failed, used keyword matching instead — "
                       f"{last_error()}")
        if res.get("table") is not None and not res["table"].empty:
            t = res["table"].copy()
            for col in ("lifetime_sgd", "amount"):
                if col in t.columns:
                    t[col] = t[col].map(money)
            st.dataframe(t, hide_index=True, width='stretch')


def page_edit():
    st.title("Edit data")
    st.caption(
        "Edit any cell, add rows with the ﹢ at the bottom of the table, or "
        "delete rows, then save. Cleaning and analytics refresh from your saved "
        "data. Columns must stay named: " + ", ".join(RAW_COLUMNS) + "."
    )

    src = Path(active_path)
    st.write(f"Editing **{src.name}** · {'uploaded copy' if uploaded else 'default dataset'}")

    raw = get_raw(active_path, os.path.getmtime(active_path))

    edited = st.data_editor(
        raw,
        key="editor",
        num_rows="dynamic",
        width="stretch",
        height=440,
    )

    # Drop rows the user added but left entirely blank before saving/exporting.
    clean = edited.dropna(how="all")

    row = st.container(horizontal=True)
    if row.button("Save changes", type="primary", icon=":material/save:"):
        clean.to_excel(active_path, index=False, sheet_name="donations")
        get_raw.clear()
        get_data.clear()
        st.session_state.pop("editor", None)
        st.toast(f"Saved {len(clean)} rows to {src.name}", icon="✅")
        st.rerun()

    row.download_button(
        "Download as Excel",
        data=to_excel_bytes(clean),
        file_name="harbour_lights_donations.xlsx",
        mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        icon=":material/download:",
    )
    st.caption(f"{len(clean)} rows. Download exports the table as shown "
               "(unsaved edits included).")


PAGES = {
    "Overview": page_overview,
    "Edit data": page_edit,
    "Data Health": page_data_health,
    "At Risk": page_at_risk,
    "Acknowledgements": page_ack,
    "Ask the data": page_ask,
}
PAGES[page]()
