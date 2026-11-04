# ENGIE Particuliers for Home Assistant

Unofficial Home Assistant integration for **ENGIE Particuliers**. It exposes current electricity and gas prices, recent consumption, billing, and can import daily history into the Energy dashboard.

It talks to the same backends as the mobile app through the [`ha-engie-api`](https://pypi.org/project/ha-engie-api/) Python client (`engie_particuliers`). Home Assistant installs that package from PyPI when you add the integration.

This is the French ENGIE Particuliers app, not ENGIE Belgium. Features that only exist in Belgium (EPEX day-ahead, Happy Hours, capacity tariff) are not implemented.

## What you get

One Home Assistant **device per energy contract**:

| Entity | Unit | Energy dashboard |
|--------|------|------------------|
| **Price kWh TTC** | `EUR/kWh` | Use this for *current price* |
| Price kWh HT | `EUR/kWh` | Optional |
| Subscription TTC | `EUR` | Monthly standing charge |
| `{cadran}` price kWh TTC / HT | `EUR/kWh` | BASE / HP / HC when ENGIE reports bands |
| **Daily consumption** | `kWh` | Last **complete** billed day (Linky D+1) |
| Daily cost TTC | `EUR` | Energy cost for that day |
| Today consumption | `kWh` | Partial current day if hourly slots exist (often unknown) |
| **Energy** | `kWh` | Cumulative daily histo — Energy dashboard grid consumption |

One **account** device:

| Entity | Notes |
|--------|--------|
| Outstanding balance | `soldeApayerV2` |
| Next invoice | Date when ENGIE reports one |
| Last invoice TTC | Amount of `derniereFacture` |
| Authenticated | Diagnostic connectivity sensor |
| Supply cut | Diagnostic, disabled by default |

Daily consumption is also written to **long-term statistics** (`engie:<contract>_energy` and `_cost`) so you can add it under Energy → Gas / Electricity grid consumption.

Missing *today* does not make that history useless: the Energy dashboard and weekly/monthly charts are built from **complete** days (yesterday and older). That is the same lag as the ENGIE app.

## Charts

**Energy dashboard** (best): Settings → Dashboards → Energy → add grid/gas consumption from the **Energy** sensor. The first poll backfills ~400 complete days. Today stays empty until tomorrow.

**Statistics graph** (built-in Lovelace), daily kWh bars from that cumulative series:

```yaml
type: statistics-graph
chart_type: bar
period: day
stat_types:
  - change
entities:
  - sensor.YOUR_CONTRACT_energy
```

**ApexCharts** from the last 31 complete days on the Daily consumption sensor (`attributes.history`):

```yaml
type: custom:apexcharts-card
graph_span: 31d
header:
  title: Electricity
series:
  - entity: sensor.YOUR_CONTRACT_last_day_kwh
    type: column
    data_generator: |
      return (entity.attributes.history || []).map((row) => {
        return [new Date(`${row.date}T00:00:00`).getTime(), row.kwh];
      });
```

## Session: login once

You log in **once** (including MFA if ENGIE asks). After that:

1. OAuth access + refresh tokens are saved in Home Assistant storage (`.storage/engie_<entry_id>`)
2. The coordinator refreshes on an interval (default 6 hours)
3. The access token is renewed with the refresh token before it expires — **no MFA on each poll**
4. If ENGIE revokes the refresh token, Home Assistant starts a re-auth flow and you enter the code again

Password is kept on the config entry only as a fallback when the refresh token dies.

## Installation

Copy `custom_components/engie` into your Home Assistant `custom_components` folder, restart, then **Settings → Devices & services → Add integration → ENGIE Particuliers**.

Home Assistant will install `ha-engie-api` automatically from `manifest.json` (`>=0.2.1`).

## Energy dashboard

1. **Settings → Dashboards → Energy**
2. Electricity grid: *Use an entity with the current price* → the **Price kWh TTC** sensor on the electricity contract
3. Gas: same, on the gas contract — your gas energy sensor must be in **kWh** (Gazpar), not m³
4. Electricity / gas consumption: pick the **Energy** sensor on each contract (or the imported `engie:…_energy` statistic)

The first poll backfills about 400 days of daily history. To redo it, call the `engie.import_history` service. `engie.clear_import_history` deletes those statistic streams.

HP / HC bands are extra sensors. The Energy dashboard still takes **one** current-price entity; pick the contract-level TTC sensor unless you split peak/off-peak yourself.

## Options

In the integration options you can change the poll interval (seconds, 5 minutes to 24 hours). Default is 6 hours.

## Gas diagnostics (Gill80 fork)

If ENGIE's app shows gas usage while this integration reports
`unknown`, inspect **Settings → Devices & services → ENGIE
Particuliers → Download diagnostics** (or use `ha_get_integration`
with diagnostics). The `gas_api` field records up to six recent
technical results per endpoint:

- `estimationConsoGazMois`: gas price estimation
- `histoGazMois`: monthly gas consumption
- `histoGazJours`: daily gas consumption

It includes HTTP status and a limited, allowlisted response shape, but
never API values, URLs, contract identifiers or tokens. A missing or
empty `gas_api` field means the installed Python library does not yet
support this feature or no gas endpoint has been called.

**Deployment caveat:** this integration still requests
`ha-engie-api>=0.2.1` from PyPI. Installing only this fork **does
not** install the matching `Gill80/ha-engie-api` branch. Both halves
must be tested together via a separately approved package/deployment
strategy. The forks do not alter an existing HA installation.

The `unit_class` metadata fix also supplies an explicit `None` for
monetary external statistics, as required by Home Assistant 2026.11.

## Limitations

- Unofficial API — ENGIE may change it
- Per-cadran HP/HC rates are not always available on every contract
- Gas €/kWh is variable and excludes the standing charge
- History import uses complete daily `histo*Jours` rows (Linky D+1). Same-day consumption is not published.
- Use at your own risk; respect ENGIE terms of service
