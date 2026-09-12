# Fixtures

`fmp_historical_envelope.json` and `fmp_bare_list_envelope.json` are hand-built
to the *shape* of an FMP historical-price response. They are not recordings of a
live call.

They exist so the suite runs with no API key and no network. They are not
evidence about FMP's field semantics. The README's spike — does `adjClose`
adjust for splits only or splits and dividends, are dividends ex-dated or
pay-dated, is `adjDividend` already split-adjusted — must be answered against
the live key, and these files re-recorded from real responses at that point.

Until then, treat any test that passes here as proving the adapter handles a
shape, not that the shape is the one your plan returns.
