"""
Real Exchange Rate (RER) App
----------------------------
Pulls monthly CPI and exchange-rate data from the IMF SDMX 3.0 API and computes

    S(t)   = units of Country 2 currency per 1 unit of Country 1 currency
           = (Country 2 LCU per USD) / (Country 1 LCU per USD)
    RER(t) = S(t) * P1(t) / P2(t)
    RER*   = average of RER(t) over valid months in the selected window
    Real Index(t) = 100 * RER(t) / RER*
"""

import io
import re
from datetime import date

import pandas as pd
import plotly.graph_objects as go
import pycountry
import requests
import streamlit as st
from plotly.subplots import make_subplots

# ----------------------------------------------------------------------------
# Settings
# ----------------------------------------------------------------------------
BASE_URL = "https://api.imf.org/external/sdmx/3.0"
AGENCY = "IMF.STA"
CPI_FLOW = "CPI"
FX_FLOW = "ER"
DATA_START = pd.Timestamp("1976-01-01")
TIMEOUT = 60
EURO_AREA = "G163"

ACCEPT_HEADERS = [
    "application/vnd.sdmx.data+csv;version=2.0.0",
    "application/vnd.sdmx.data+json;version=2.0.0",
    "application/json",
]


class IMFError(Exception):
    """Raised when the IMF API returns an error or an unreadable response."""

    def __init__(self, message: str, url: str):
        super().__init__(message)
        self.url = url


# ----------------------------------------------------------------------------
# Country list
# ----------------------------------------------------------------------------
def country_options() -> dict:
    """Return {display label: ISO3 code}, with the Euro area first."""
    options = {f"Euro area ({EURO_AREA})": EURO_AREA}
    entries = []
    for c in pycountry.countries:
        name = getattr(c, "common_name", None) or c.name
        entries.append((name, c.alpha_3))
    for name, code in sorted(entries):
        options[f"{name} ({code})"] = code
    return options


def short_name(label: str) -> str:
    """'United Kingdom (GBR)' -> 'United Kingdom'."""
    return re.sub(r"\s*\([A-Z0-9]{3,4}\)$", "", label)


# ----------------------------------------------------------------------------
# IMF download and parsing
# ----------------------------------------------------------------------------
def build_url(flow: str, key: str) -> str:
    return f"{BASE_URL}/data/dataflow/{AGENCY}/{flow}/+/{key}"


def parse_period(value) -> pd.Timestamp:
    """Handle IMF month formats such as '2024-M01', '2024M01', '2024-01'."""
    m = re.match(r"^\s*(\d{4})-?M?(\d{1,2})", str(value))
    if not m:
        return pd.NaT
    year, month = int(m.group(1)), int(m.group(2))
    if not 1 <= month <= 12:
        return pd.NaT
    return pd.Timestamp(year=year, month=month, day=1)


def to_monthly_series(periods, values) -> pd.Series:
    s = pd.Series(
        pd.to_numeric(pd.Series(list(values)), errors="coerce").values,
        index=pd.DatetimeIndex([parse_period(p) for p in periods]),
    )
    s = s[s.index.notna()].dropna()
    s = s.groupby(level=0).mean().sort_index()
    return s


def parse_csv(text: str) -> pd.Series:
    df = pd.read_csv(io.StringIO(text))
    cols = {c.upper(): c for c in df.columns}
    if "TIME_PERIOD" not in cols or "OBS_VALUE" not in cols:
        raise ValueError("CSV response has no TIME_PERIOD / OBS_VALUE columns")
    return to_monthly_series(df[cols["TIME_PERIOD"]], df[cols["OBS_VALUE"]])


def parse_sdmx_json(payload: dict) -> pd.Series:
    data = payload.get("data", payload)
    structure = data.get("structure")
    if structure is None:
        structures = data.get("structures") or []
        structure = structures[0] if structures else None
    if structure is None:
        raise ValueError("JSON response has no structure section")

    obs_dims = structure["dimensions"]["observation"]
    time_pos = next(i for i, d in enumerate(obs_dims) if d["id"] == "TIME_PERIOD")
    time_values = [v.get("id") or v.get("value") for v in obs_dims[time_pos]["values"]]

    periods, values = [], []
    for dataset in data.get("dataSets", []):
        for series in dataset.get("series", {}).values():
            for obs_key, obs in series.get("observations", {}).items():
                idx = int(str(obs_key).split(":")[time_pos])
                periods.append(time_values[idx])
                values.append(obs[0] if isinstance(obs, list) and obs else None)
            break  # one series per key is expected
    return to_monthly_series(periods, values)


@st.cache_data(ttl=24 * 3600, show_spinner=False)
def download_series(flow: str, key: str) -> pd.Series:
    """Download one IMF series. Errors are raised (and therefore not cached)."""
    url = build_url(flow, key)
    last_problem = "No response"
    for accept in ACCEPT_HEADERS:
        headers = {"Accept": accept, "User-Agent": "real-exchange-rate-app/1.0"}
        try:
            resp = requests.get(url, headers=headers, timeout=TIMEOUT)
        except requests.RequestException as exc:
            raise IMFError(f"Network error: {exc}", url) from exc

        if resp.status_code == 404:
            return pd.Series(dtype=float)  # SDMX "no results found"
        if resp.status_code in (406, 415):
            last_problem = f"HTTP {resp.status_code} for Accept: {accept}"
            continue
        if resp.status_code != 200:
            raise IMFError(f"HTTP {resp.status_code}: {resp.text[:500]}", url)

        text = resp.text.strip()
        if not text:
            return pd.Series(dtype=float)
        try:
            if text.startswith("{"):
                return parse_sdmx_json(resp.json())
            return parse_csv(text)
        except Exception as exc:  # unreadable format: try the next one
            last_problem = f"Could not parse response ({accept}): {exc}"
            continue
    raise IMFError(last_problem, url)


def fetch(flow: str, key: str, problems: list) -> pd.Series:
    """Download a series and log any problem instead of crashing."""
    try:
        s = download_series(flow, key)
    except IMFError as exc:
        problems.append(("error", key, exc.url, str(exc)))
        return pd.Series(dtype=float)
    except Exception as exc:
        problems.append(("error", key, build_url(flow, key), repr(exc)))
        return pd.Series(dtype=float)
    if s.empty:
        problems.append(("empty", key, build_url(flow, key), "No observations returned"))
    return s


def fx_per_usd(iso3: str, problems: list) -> pd.Series:
    """Local currency per USD. The U.S. is exactly 1 by definition."""
    if iso3 == "USA":
        months = pd.date_range(DATA_START, pd.Timestamp(date.today()), freq="MS")
        return pd.Series(1.0, index=months)
    return fetch(FX_FLOW, f"{iso3}.XDC_USD.PA_RT.M", problems)


# ----------------------------------------------------------------------------
# Calculations
# ----------------------------------------------------------------------------
def build_panel(fx1: pd.Series, fx2: pd.Series, p1: pd.Series, p2: pd.Series) -> pd.DataFrame:
    df = pd.concat(
        {"FX1_LCU_per_USD": fx1, "FX2_LCU_per_USD": fx2, "P1": p1, "P2": p2},
        axis=1,
        sort=True,
    )
    df = df[df.index >= DATA_START]
    df["S"] = df["FX2_LCU_per_USD"] / df["FX1_LCU_per_USD"]
    df["RER"] = df["S"] * df["P1"] / df["P2"]
    return df


def window_calc(df: pd.DataFrame, start: pd.Timestamp, end: pd.Timestamp):
    """Slice to the window, compute RER* over valid months, and the Real Index."""
    w = df.loc[start:end].copy()
    valid = w[["S", "P1", "P2", "RER"]].notna().all(axis=1)
    rer_star = w.loc[valid, "RER"].mean() if valid.any() else float("nan")
    w["RER_star"] = rer_star
    w["Real_Index"] = 100 * w["RER"] / rer_star
    return w, rer_star, int(valid.sum())


def export_frame(w: pd.DataFrame, n1: str, n2: str) -> pd.DataFrame:
    out = pd.DataFrame(
        {
            "date": w.index.strftime("%Y-%m"),
            f"{n1} LCU per USD": w["FX1_LCU_per_USD"],
            f"{n2} LCU per USD": w["FX2_LCU_per_USD"],
            f"S ({n2} currency per 1 {n1} currency)": w["S"],
            f"P1 ({n1} CPI)": w["P1"],
            f"P2 ({n2} CPI)": w["P2"],
            "RER": w["RER"],
            "RER* (window average)": w["RER_star"],
            "Real Index": w["Real_Index"],
        }
    )
    return out.reset_index(drop=True)


# ----------------------------------------------------------------------------
# Charts
# ----------------------------------------------------------------------------
def make_chart(w: pd.DataFrame, mode: str, n1: str, n2: str) -> go.Figure:
    s_label = f"{n2} currency per 1 {n1} currency"
    if mode == "Both":
        fig = make_subplots(specs=[[{"secondary_y": True}]])
        fig.add_trace(go.Scatter(x=w.index, y=w["Real_Index"], name="Real Index (avg = 100)"), secondary_y=False)
        fig.add_trace(go.Scatter(x=w.index, y=w["S"], name=f"Nominal S ({s_label})"), secondary_y=True)
        fig.update_yaxes(title_text="Real Index (window avg = 100)", secondary_y=False)
        fig.update_yaxes(title_text=f"Nominal S: {s_label}", secondary_y=True)
        fig.add_hline(y=100, line_dash="dot", line_color="gray")
    elif mode == "Real":
        fig = go.Figure(go.Scatter(x=w.index, y=w["Real_Index"], name="Real Index"))
        fig.update_yaxes(title_text="Real Index (window avg = 100)")
        fig.add_hline(y=100, line_dash="dot", line_color="gray")
    else:
        fig = go.Figure(go.Scatter(x=w.index, y=w["S"], name="Nominal S"))
        fig.update_yaxes(title_text=f"Nominal S: {s_label}")
    fig.update_layout(
        hovermode="x unified",
        height=520,
        margin=dict(l=20, r=20, t=40, b=20),
        legend=dict(orientation="h", yanchor="bottom", y=1.02, x=0),
    )
    fig.update_xaxes(title_text="Month")
    return fig


# ----------------------------------------------------------------------------
# App
# ----------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Real Exchange Rate App", layout="wide")
    st.title("Real Exchange Rate App")
    st.caption(
        "Monthly IMF data. S = units of Country 2 currency per 1 unit of Country 1 currency; "
        "RER = S × P1 / P2; Real Index = 100 × RER / RER*, where RER* is the average RER "
        "over valid months in the selected window."
    )

    options = country_options()
    labels = list(options.keys())
    default_1 = next((l for l in labels if l.startswith("United Kingdom")), labels[0])
    default_2 = next((l for l in labels if l.startswith("United States (")), labels[1])

    col1, col2 = st.columns(2)
    label1 = col1.selectbox("Country 1", labels, index=labels.index(default_1))
    label2 = col2.selectbox("Country 2", labels, index=labels.index(default_2))
    iso1, iso2 = options[label1], options[label2]
    n1, n2 = short_name(label1), short_name(label2)

    if iso1 == iso2:
        st.warning("Choose two different countries.")
        st.stop()

    problems: list = []
    with st.spinner("Downloading IMF data..."):
        p1 = fetch(CPI_FLOW, f"{iso1}.CPI._T.IX.M", problems)
        p2 = fetch(CPI_FLOW, f"{iso2}.CPI._T.IX.M", problems)
        fx1 = fx_per_usd(iso1, problems)
        fx2 = fx_per_usd(iso2, problems)

    for kind, key, url, msg in problems:
        if kind == "error":
            st.error(f"IMF request failed for **{key}**\n\nURL: `{url}`\n\nDetails: {msg}")
        else:
            st.warning(f"No IMF data for **{key}** (URL: `{url}`).")

    df = build_panel(fx1, fx2, p1, p2)
    valid_months = df.index[df[["S", "P1", "P2", "RER"]].notna().all(axis=1)]
    if len(valid_months) == 0:
        st.error(
            "No months have CPI and exchange-rate data for both countries, so the RER "
            "cannot be computed. Try another pair or check the messages above."
        )
        st.stop()

    months = pd.date_range(valid_months.min(), valid_months.max(), freq="MS")
    month_labels = [m.strftime("%Y-%m") for m in months]
    start_label, end_label = st.select_slider(
        "Sample period (start month, end month)",
        options=month_labels,
        value=(month_labels[0], month_labels[-1]),
    )
    start, end = pd.Timestamp(start_label + "-01"), pd.Timestamp(end_label + "-01")

    w, rer_star, n_valid = window_calc(df, start, end)
    if n_valid == 0:
        st.error("No valid months in the selected window. Widen the sample period.")
        st.stop()

    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Valid months in window", f"{n_valid}")
    m2.metric("RER* (window average)", f"{rer_star:,.6g}")
    m3.metric("Average Real Index", f"{w['Real_Index'].mean():.4f}")
    m4.metric(f"Latest S ({end_label})", f"{w['S'].dropna().iloc[-1]:,.6g}" if w["S"].notna().any() else "n/a")

    if n_valid < len(w):
        st.info(f"{len(w) - n_valid} month(s) in the window have missing data and are excluded from RER*.")

    mode = st.radio("Chart", ["Real", "Nominal", "Both"], horizontal=True)
    st.plotly_chart(make_chart(w, mode, n1, n2), width="stretch")

    export = export_frame(w, n1, n2)
    st.download_button(
        "Download CSV (selected period)",
        data=export.to_csv(index=False).encode("utf-8"),
        file_name=f"rer_{iso1}_{iso2}_{start_label}_{end_label}.csv",
        mime="text/csv",
    )

    with st.expander("Data table"):
        st.dataframe(export, width="stretch", hide_index=True)

    with st.expander("Data sources and requests"):
        st.markdown(
            f"- CPI: `{build_url(CPI_FLOW, f'{{ISO3}}.CPI._T.IX.M')}`\n"
            f"- Exchange rates: `{build_url(FX_FLOW, f'{{ISO3}}.XDC_USD.PA_RT.M')}`\n"
            "- IMF exchange rates are local currency per USD; the U.S. is set to 1.\n"
            "- S = (Country 2 LCU per USD) ÷ (Country 1 LCU per USD)."
        )


if __name__ == "__main__":
    main()
