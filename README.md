# real-exchange-rate-app
# Real Exchange Rate App

A Streamlit app that computes the real exchange rate (RER) between any two IMF countries, including the Euro area, using monthly IMF CPI and exchange-rate data from January 1976 where available.

## Economics

- **S(t)**: units of Country 2 currency per 1 unit of Country 1 currency.
  IMF rates are local currency per USD, so S(t) = (Country 2 LCU per USD) ÷ (Country 1 LCU per USD). The U.S. is set to exactly 1 LCU per USD.
- **RER(t)** = S(t) × P1(t) / P2(t), where P1 and P2 are monthly CPIs.
- **RER\***: average of RER(t) over months in the selected window where S, P1, and P2 are all available. Recomputed whenever the dates change.
- **Real Index(t)** = 100 × RER(t) / RER\*, which averages 100 over the selected window.
- Nominal S(t) is plotted in actual currency units and is not normalized.

## Features

- Country 1 and Country 2 selectors (all ISO countries plus the Euro area, IMF code `G163`)
- Month-level sample period selector
- Interactive Plotly charts: Real, Nominal, or Both (secondary y-axis)
- CSV download of every observation and calculation in the selected window
- Graceful handling of missing data and API errors, with the request URL and error shown in the app

## Data

IMF SDMX 3.0 API: `https://api.imf.org/external/sdmx/3.0`

| Series | Dataflow | Key |
|---|---|---|
| CPI | `IMF.STA/CPI` | `{ISO3}.CPI._T.IX.M` |
| Exchange rate (LCU per USD) | `IMF.STA/ER` | `{ISO3}.XDC_USD.PA_RT.M` |

Coverage varies by country. Euro area data begins around 1999. Months with missing inputs are excluded from RER\*.

## Files

- `app.py`: the Streamlit app
- `requirements.txt`: Python dependencies
- `README.md`: this file

## Deploy

1. Put all three files at the top level of a public GitHub repository.
2. At share.streamlit.io, choose **Create app → Yup, I have an app**, select the repository, branch `main`, and file `app.py`.
3. Under **Advanced settings**, choose Python 3.12. No secrets are needed.
4. Click **Deploy**.
