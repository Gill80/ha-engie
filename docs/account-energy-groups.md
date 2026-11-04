# ENGIE multi-account / per-energy devices (prototype)

## Problem and approach

ENGIE `getInfoUsers` lists commercial energy contracts by *compte contrat*
(CC), but `recupererDonneesReleveV2` can expose a physical gas PCE on a CC
listed primarily as electricity. Treating the commercial contract's energy
type as the sole source of truth causes the wrong gas account to be queried.

This branch introduces **one independently polled meter source per CC,
energy and detected physical meter**, while retaining existing contract
sensors and their unique IDs for backward compatibility.

## Source discovery

- Iterate every CC for every ENGIE business partner.
- Read `getReleve` for that CC.
- Create an electricity source when a declared electricity contract exists
  **or** `contratElec` contains a valid PDL.
- Create a gas source when a declared gas contract exists **or**
  `contratGaz` contains a valid PCE (the response sometimes calls it
  `idPDL` even for gas).
- Obtain historical values using that **same CC**, not another CC that
  happens to carry a gas commercial contract.
- Sources without history are still displayed for selection; an old gas
  meter with no usage is **not** silently considered the active meter.
- A configured PCE digest is used only to mark the corresponding gas
  source, never to copy readings from another CC.

The source ID is a SHA-256-based stable identifier derived from
partner + CC + energy + metering reference, and never contains the raw
number in Home Assistant entity IDs.

## Device names

Examples, all references illustrative:

- `ENGIE · Électricité · CC …1234 · PDL …8951`
- `ENGIE · Gaz · CC …1234 · PCE …7317`
- `ENGIE · Gaz · CC …5678 · PCE …3344`

A name identifies the **energy**, the **account** and, when available, the
**meter**. The metering identifier's final 4 characters are shown; never
the full PCE/PDL. If a reference is not available, the group still uses the
account and energy. Device IDs do not depend on the names or ordering.

Each group offers the applicable tariff, last-day use, last-day cost,
subscription and current-day readings. No synthetic accumulating energy
counter is created for these source groups: summing a moving 31-day window
is **not** a valid lifetime total.

## Home Assistant options

- `meter_source_mode = all` (default): monitor every detected source.
- `meter_source_mode = selected`: monitor only IDs from
  `selected_meter_sources` (multi-select in the ENGIE options flow).
  Discovery still lists all sources, but unselected sources do not make
  additional source-specific history calls.
- `gas_pce_sha256` remains supported for the existing legacy contract
  mapping. No full PCE is persisted in options.

The options UI builds its choices from the coordinator's discovered sources.
The user may rename any HA device from its device page, without changing
the underlying stable identifiers.

## Migration and constraints

- Existing `ContractSnapshot` entries, original contract IDs and sensor
  unique IDs remain unchanged; existing dashboard cards continue to work.
- New source devices have **different unique IDs**.
- Automatic long-term statistics still import only legacy contracts,
  **not** the new source groups. This deliberately avoids double-counting
  energy or costs before a controlled statistics migration.
- This is a development branch, not yet deployed to production. Validation
  should verify new device discovery and options flow on Home Assistant
  before merging, and inspect any custom dashboard dependencies before
  renaming existing entities.
- No new API writes; meter history and account discovery use read-only GETs.
- The historical gas price obtained from `consoEuroTTC / conso` is an
  *observed price estimate*, not a contractual tariff guarantee.
- An old meter with no consumption may still have an active subscription;
  this is not proof of termination.
