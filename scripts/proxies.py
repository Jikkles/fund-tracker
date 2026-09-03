"""
Map each tracker fund to a market proxy we can price for free.

WHY LSE-LISTED GBP ETFs RATHER THAN RAW INDICES
------------------------------------------------
The obvious approach is to price the underlying index: FTSE 100 for a FTSE 100
tracker, S&P 500 for a US tracker. That works for UK funds and breaks for
everything else, because a UK investor in a US tracker earns the index move
*plus* the USD/GBP move. In a week where the S&P rises 2% and sterling
strengthens 2%, the raw index says +2% and the investor got roughly nothing.

So wherever possible we price a London-listed, GBP-denominated accumulating
ETF tracking the same index. Its price already embeds the currency effect and
is what a UK holder actually experiences. That is a genuine accuracy gain over
estimating from headline index moves.

Bond funds use ETF proxies for the same reason and one more: bond fund NAVs
move on price, not yield, and converting a yield change into a price change
requires the fund's duration, which we do not reliably have.

CONFIDENCE LABELS
-----------------
  exact    - proxy tracks the same index; expect close agreement
  close    - proxy tracks a very similar index (ex-UK vs ex-nothing, etc.)
  loose    - proxy is directionally right but the index differs materially

Nothing here is a substitute for the fund's own factsheet. Every generated
figure says so.
"""

from __future__ import annotations

from typing import NamedTuple


class Proxy(NamedTuple):
    ticker: str          # Yahoo Finance symbol
    label: str           # human-readable, appears in the note
    confidence: str      # exact | close | loose
    note: str = ""       # caveat appended to the generated note


# ---------------------------------------------------------------------------
# Tracker funds -> proxy. Only funds listed here get a computed figure.
# Everything else is marked "not yet verified" with index context attached.
# ---------------------------------------------------------------------------
FUND_PROXIES: dict[str, Proxy] = {
    # --- UK equity ---------------------------------------------------------
    "lg-uk-100-index": Proxy(
        "ISF.L", "iShares Core FTSE 100 UCITS ETF (GBP)", "exact"),
    "lg-uk-index": Proxy(
        "VUKE.L", "Vanguard FTSE 100 UCITS ETF (GBP)", "close",
        "Fund tracks FTSE All-Share; proxy is FTSE 100, so mid/small-cap "
        "divergence is not captured."),
    "lg-uk-mid-cap-index": Proxy(
        "VMID.L", "Vanguard FTSE 250 UCITS ETF (GBP)", "close",
        "Fund excludes investment trusts; the proxy includes them."),
    "hsbc-ftse-250-index": Proxy(
        "VMID.L", "Vanguard FTSE 250 UCITS ETF (GBP)", "exact"),
    "lg-future-world-esg-uk": Proxy(
        "VUKE.L", "Vanguard FTSE 100 UCITS ETF (GBP)", "loose",
        "Fund is ESG-tilted and will diverge from a plain UK index, "
        "particularly when miners, tobacco or energy drive the market."),

    # --- US / global equity ------------------------------------------------
    "lg-us-index": Proxy(
        "VUSA.L", "Vanguard S&P 500 UCITS ETF (GBP)", "close",
        "Fund tracks FTSE USA; proxy is S&P 500. Very similar universe. "
        "GBP-denominated, so the figure includes the USD/GBP effect a UK "
        "holder actually experiences."),
    "fidelity-index-world": Proxy(
        "SWDA.L", "iShares Core MSCI World UCITS ETF (GBP)", "exact"),
    "lg-international-index": Proxy(
        "SWDA.L", "iShares Core MSCI World UCITS ETF (GBP)", "close",
        "Fund tracks FTSE World ex-UK; proxy is MSCI World, which includes "
        "the UK at a small weight."),
    "lg-future-world-esg-developed": Proxy(
        "SWDA.L", "iShares Core MSCI World UCITS ETF (GBP)", "loose",
        "Fund is ESG-tilted and optimised; expect divergence from a plain "
        "developed-world index."),
    "vanguard-global-small-cap-index": Proxy(
        "WLDS.L", "iShares MSCI World Small Cap UCITS ETF (GBP)", "exact"),

    # --- Europe / Asia -----------------------------------------------------
    "lg-european-index": Proxy(
        "VERX.L", "Vanguard FTSE Developed Europe ex-UK UCITS ETF (GBP)",
        "exact"),
    "ishares-japan-equity-index": Proxy(
        "IJPA.L", "iShares Core MSCI Japan IMI UCITS ETF (GBP)", "close",
        "Fund tracks FTSE Japan; proxy is MSCI Japan IMI, which reaches "
        "further down the cap scale."),
    "ishares-pacific-ex-japan-index": Proxy(
        "CPJ1.L", "iShares Core MSCI Pacific ex-Japan UCITS ETF (GBP)",
        "close",
        "Fund tracks FTSE Asia-Pacific ex-Japan, which includes emerging "
        "Asia; the MSCI Pacific ex-Japan proxy is developed-only, so Korea "
        "and Taiwan exposure differs."),

    # --- Bonds -------------------------------------------------------------
    "lg-all-stocks-gilt-index": Proxy(
        "IGLT.L", "iShares Core UK Gilts UCITS ETF (GBP)", "exact"),
    "ishares-corporate-bond-index": Proxy(
        "SLXX.L", "iShares Core GBP Corporate Bond UCITS ETF (GBP)", "close",
        "Fund tracks iBoxx £ Non-Gilts with partial replication; proxy is "
        "the corporate-only sleeve."),
    "vanguard-global-bond-index": Proxy(
        "VAGP.L", "Vanguard Global Aggregate Bond UCITS ETF GBP-hedged",
        "exact"),
    "vanguard-global-corporate-bond-index": Proxy(
        "VCPA.L", "Vanguard Global Corporate Bond UCITS ETF GBP-hedged",
        "exact"),
}


# ---------------------------------------------------------------------------
# Market context tickers. Fetched every run and attached to the notes of
# ACTIVE funds by group, so an unverified fund still carries real numbers
# about the market it operates in.
# ---------------------------------------------------------------------------
CONTEXT_TICKERS: dict[str, str] = {
    "FTSE 100": "^FTSE",
    "FTSE 250": "^FTMC",
    "S&P 500": "^GSPC",
    "Nasdaq 100": "^NDX",
    "Euro STOXX 50": "^STOXX50E",
    "Nikkei 225": "^N225",
    "Hang Seng": "^HSI",
    "Gold (USD/oz)": "GC=F",
    "Brent crude": "BZ=F",
    "US 10yr yield": "^TNX",
    "GBP/USD": "GBPUSD=X",
}

# Which context lines are most relevant to each fund group.
#
# Keys must match data/funds.json `categories`. The desk moved from seven
# equal buckets to HL's own sector split plus the asset classes HL does not
# shortlist (property, specialist, cash), because ten-per-bucket was forcing
# Japan, China and emerging markets into one row while ten near-identical
# Europe funds sat in another.
GROUP_CONTEXT: dict[str, list[str]] = {
    "uk-growth":       ["FTSE 100", "FTSE 250", "GBP/USD"],
    "uk-income":       ["FTSE 100", "GBP/USD"],
    "uk-smaller":      ["FTSE 250", "FTSE 100", "GBP/USD"],
    "us":              ["S&P 500", "Nasdaq 100", "US 10yr yield", "GBP/USD"],
    "europe":          ["Euro STOXX 50", "FTSE 100", "GBP/USD"],
    "japan":           ["Nikkei 225", "GBP/USD"],
    "asia":            ["Hang Seng", "Nikkei 225", "GBP/USD"],
    "em":              ["Hang Seng", "S&P 500", "GBP/USD"],
    "global":          ["S&P 500", "FTSE 100", "Euro STOXX 50", "GBP/USD"],
    "property":        ["FTSE 100", "US 10yr yield", "GBP/USD"],
    "specialist":      ["Nasdaq 100", "Gold (USD/oz)", "Brent crude", "GBP/USD"],
    "bonds-gov":       ["US 10yr yield", "GBP/USD"],
    "bonds-strategic": ["US 10yr yield", "Brent crude", "GBP/USD"],
    "cash":            ["US 10yr yield", "GBP/USD"],
    "mixed":           ["FTSE 100", "S&P 500", "US 10yr yield", "Gold (USD/oz)"],
    "total-return":    ["FTSE 100", "S&P 500", "Gold (USD/oz)", "US 10yr yield"],
}
