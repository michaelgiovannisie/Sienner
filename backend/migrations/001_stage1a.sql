-- Stage 1a schema: raw bars and the coverage ledger.
--
-- Deliberately absent until Stage 1b: securities, corporate_actions, quarantine.
-- Stage 1a stores what it fetches and nothing derived.

BEGIN;

-- ---------------------------------------------------------------------------
-- daily_bars: raw OHLCV as the provider reported it. Never back-adjusted.
--
-- This table is append-only by construction. A corporate action does not
-- rewrite history here; it lands in corporate_actions (Stage 1b) and the
-- return series is derived on read. That is the whole reason the cache can be
-- trusted indefinitely.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS daily_bars (
    ticker      text          NOT NULL,
    bar_date    date          NOT NULL,
    open        numeric(18,6) NOT NULL,
    high        numeric(18,6) NOT NULL,
    low         numeric(18,6) NOT NULL,
    close       numeric(18,6) NOT NULL,
    volume      bigint        NOT NULL,
    source      text          NOT NULL,
    ingested_at timestamptz   NOT NULL DEFAULT now(),

    PRIMARY KEY (ticker, bar_date),

    -- Hard-reject rules from the README, enforced at the storage layer too so
    -- that a bug in application validation cannot persist a bad row.
    CONSTRAINT daily_bars_prices_positive
        CHECK (open > 0 AND high > 0 AND low > 0 AND close > 0),
    CONSTRAINT daily_bars_volume_nonnegative
        CHECK (volume >= 0),
    CONSTRAINT daily_bars_high_is_high
        CHECK (high >= low AND high >= open AND high >= close),
    CONSTRAINT daily_bars_low_is_low
        CHECK (low <= open AND low <= close)
);

COMMENT ON TABLE daily_bars IS
    'Raw OHLCV per ticker-date. Immutable once written. Never back-adjusted.';

-- Immutability is a design decision, so make the database refuse to violate it
-- rather than trusting every future caller to remember.
CREATE OR REPLACE FUNCTION daily_bars_reject_update() RETURNS trigger AS $$
BEGIN
    RAISE EXCEPTION
        'daily_bars is immutable: attempted UPDATE on %/%. Corrections belong in corporate_actions or quarantine, not here.',
        OLD.ticker, OLD.bar_date;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS daily_bars_no_update ON daily_bars;
CREATE TRIGGER daily_bars_no_update
    BEFORE UPDATE ON daily_bars
    FOR EACH ROW EXECUTE FUNCTION daily_bars_reject_update();

-- ---------------------------------------------------------------------------
-- fetch_coverage: which ticker-ranges have been successfully retrieved.
--
-- This ledger is what distinguishes three states that daily_bars alone cannot:
--
--   not fetched            -- no coverage row spans the date
--   fetched, no bar exists -- a coverage row spans it, no daily_bars row
--                             (market holiday, or before listing)
--   fetched, quarantined   -- Stage 1b; a quarantine row spans it
--
-- Without this, "we have no bar for 2020-07-03" and "we never asked about
-- 2020-07-03" are indistinguishable, and every backfill becomes a rewrite
-- instead of a no-op.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS fetch_coverage (
    id          bigserial     PRIMARY KEY,
    ticker      text          NOT NULL,
    range_start date          NOT NULL,
    range_end   date          NOT NULL,
    source      text          NOT NULL,
    fetched_at  timestamptz   NOT NULL DEFAULT now(),
    bars_written integer      NOT NULL DEFAULT 0,

    CONSTRAINT fetch_coverage_range_ordered CHECK (range_end >= range_start)
);

CREATE INDEX IF NOT EXISTS fetch_coverage_ticker_range_idx
    ON fetch_coverage (ticker, range_start, range_end);

COMMENT ON TABLE fetch_coverage IS
    'Ticker-ranges successfully retrieved from a provider, and when. Append-only.';

COMMIT;
