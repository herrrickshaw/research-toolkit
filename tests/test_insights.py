"""Tests for insights/privacy.py and the pure claim-extraction helpers of
insights/build_insights.py. Synthetic text only."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "insights"))
import privacy  # noqa: E402

bi = pytest.importorskip("build_insights")


# ---- privacy.is_sensitive -------------------------------------------------------

@pytest.mark.parametrize("title,head", [
    ("HDFC account statement Jan", ""),
    ("notes", "Your PAN ABCDE1234F is linked"),
    ("notes", "card 1234 5678 9012 ending"),
    ("e-ticket confirmation", ""),
    ("Offer Letter - Acme", ""),
    ("Quarterly_Statement_2024", ""),
    ("my NDA draft", ""),
    ("doc", "Please find the contract note for your trades"),
])
def test_sensitive_documents_are_dropped(title, head):
    assert privacy.is_sensitive(title, head)


@pytest.mark.parametrize("title,head", [
    ("Global Lithium Market Outlook", "The lithium market is valued at USD 20 billion"),
    ("Agenda for the meeting", "an agenda of topics"),          # 'nda' inside a word
    ("Calendar of events", ""),
])
def test_literature_is_kept(title, head):
    assert not privacy.is_sensitive(title, head)


def test_owner_name_only_blocks_literature_kind():
    assert privacy.is_sensitive("x", "written by Umashankar")
    assert not privacy.is_sensitive("x", "written by Umashankar", kind="own")


def test_only_first_3000_chars_of_body_are_scanned():
    head = "a" * 3000 + " bank statement"
    assert not privacy.is_sensitive("x", head)


# ---- claim extraction -------------------------------------------------------------

def test_market_size_claim_parsed_with_unit_and_year():
    text = "The global electric vehicle market was valued at USD 1.5 trillion in 2030 estimates."
    claims = bi.extract_claims(text)
    assert len(claims) == 1
    c = claims[0]
    assert c["kind"] == "market_size"
    assert c["currency"] == "USD" and c["value"] == pytest.approx(1.5e12)
    assert c["subject"] == "global electric vehicle" and c["year"] == 2030


@pytest.mark.parametrize("money,cur,value", [
    ("Rs 5 lakh crore", "INR", 5e12),
    ("₹1,00,000 crore", "INR", 1e12),
    ("$250 million", "USD", 2.5e8),
    ("€3.2 bn", "EUR", 3.2e9),
])
def test_currency_and_unit_normalisation(money, cur, value):
    claims = bi.extract_claims(f"The Indian dairy market is estimated at {money} by 2027.")
    assert [(c["currency"], c["value"]) for c in claims] == [(cur, pytest.approx(value))]


def test_money_without_market_context_is_ignored():
    assert bi.extract_claims("The company raised USD 5 million from investors.") == []


def test_cagr_claim():
    claims = bi.extract_claims("The solar inverter market will grow at a CAGR of 12.5% over the period.")
    assert [(c["kind"], c["value"], c["subject"]) for c in claims] == [("cagr_pct", 12.5, "solar inverter")]


def test_claims_capped_at_40():
    text = " ".join(f"The tea market is valued at USD {i} million." for i in range(1, 60))
    assert len(bi.extract_claims(text)) == 40


def test_subject_of_strips_stopwords_and_returns_last_three():
    w = "the very large Asia Pacific specialty coffee market is worth $2 bn"
    assert bi.subject_of(w, w.index("$")) == "pacific specialty coffee"
    assert bi.subject_of("the market was $2 bn", 15) is None


def test_clean_title_and_guess_year():
    assert bi.clean_title("ABCD1234_My Paper") == "My Paper"
    assert bi.clean_title(None) == ""
    assert bi.guess_year("Copyright 2019, revised 2021; see 1890 and 2999") == 2021
    assert bi.guess_year("no year here") is None
