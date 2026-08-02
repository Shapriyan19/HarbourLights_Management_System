# Harbour Lights — Donor Management Tool

A lightweight donor-management app for **Jonathan**, the non-technical admin lead
of a small social service agency. It turns a messy, hand-entered donation
spreadsheet into something he can **trust and act on**, and works as an ongoing
tool rather than a one-off clean-up.

It delivers the four capabilities in the brief, plus in-app editing and export:

| Capability | Page | How it works |
|---|---|---|
| **Trust the data** | Data Health | Flags probable duplicates, missing fields, invalid emails, non-SGD gifts, and anomalous amounts. Nothing auto-changes — Jonathan approves each fix. **Merge** now consolidates a duplicate onto one donor ID and saves it back to the sheet. |
| **Spot lapsing donors** | At Risk | Monthly donors silent > 45 days, biggest givers first. |
| **Keep up acknowledgements** | Acknowledgements | Every un-thanked gift, with a one-click AI-drafted thank-you to review and send. |
| **Answer the director** | Ask the data | Plain-English questions ("top 10 donors", "total for the Building Fund") plus quick-report buttons. |
| **Edit & export** | Edit data | An editable grid of the whole spreadsheet — fix cells, add or delete rows, **Save** back to the active file, or **Download as Excel**. |

## How to run

```bash
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/streamlit run app/dashboard.py
```

Then open the URL it prints. The app loads `data/donations.xlsx` by default;
Jonathan can drag-drop a newer `.xlsx` in the sidebar at any time — an upload
becomes the active dataset for every page (a throwaway copy is kept, so the
default file is never overwritten by an import).

**AI is optional (Google Gemini).** Without a key the app runs fully, using
template thank-you notes and keyword-based Q&A. To enable live drafting and
natural-language questions, copy `.env.example` to `.env` and add your key
([get one here](https://aistudio.google.com/apikey)):

```bash
cp .env.example .env
# then edit .env:  GEMINI_API_KEY=your-key-here
```

Restart Streamlit and the sidebar will show "🟢 AI connected (Gemini)".

## Approach, and why it fits Jonathan

- **Deterministic core, thin AI layer.** Everything that must be *correct or
  auditable* — money totals, dates, duplicate keys — is plain Python
  (`app/cleaning.py`), never an LLM. The model (`app/ai.py`) only touches
  *language and intent*: drafting thank-yous and turning a question into a safe,
  structured query whose numbers are still computed from the clean data. A
  charity cannot have a model hallucinate a donation total or silently merge two
  real people.
- **Low switching cost.** The spreadsheet stays the input. The tool imports it;
  Jonathan does not have to abandon Excel or re-key anything.
- **Built for a non-technical user.** Task-focused pages and buttons, not a blank
  chat box. Deliberately *not* a general chatbot — see the trade-off below.
- **Scales to 600 donors.** SQLite-ready model, vectorised pandas, and
  clustering that stays fast well beyond today's ~60 donors.
- **Responsible with data.** PII stays local; only the minimum fields (first
  name, amount, campaign) are ever sent to the model; duplicates are *suggested*,
  never auto-merged; anonymous / no-email donors are flagged do-not-contact.

## Biggest trade-off

**A scoped "Ask the data" box instead of a full conversational chatbot.** A chat
box is more intimidating for a non-technical user and — worse — invites the model
to *compute* figures it might get wrong. Instead the LLM only interprets the
question; the actual numbers come from the clean data. Less flashy, far more
trustworthy for money.

## What I consciously omitted

- No live email-inbox integration or auto-sending — drafts only, human in the loop.
- No *automatic* duplicate merging — merges are one click, but always Jonathan's
  decision, never silent.
- No multi-user auth/roles (5 staff, one admin today).

## Layout

```
app/cleaning.py      deterministic parsing, validation, dedup, rollups
app/ai.py            thank-you drafting + NL->query (with no-key fallbacks)
app/dashboard.py     Streamlit UI (6 pages: incl. Edit data + export)
data/donations.xlsx  the provided donor spreadsheet (default input)
```
