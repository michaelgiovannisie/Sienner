-- Stage 1b: corporate actions and the quarantine ledger.
--
-- The Stage 1a README claim was "once computed, r_t never changes". That holds
-- against FUTURE corporate actions, which is the property the cache design
-- needs. It does NOT hold against CORRECTIONS to past actions - a late-announced
-- dividend, a restated ratio, a special dividend reclassified. Append-only alone
-- gives an audit trail but no resolution rule for "this ex-date now has two
-- rows", so this schema carries an explicit supersedes chain.

BEGIN;

-- ---------------------------------------------------------------------------
-- corporate_actions: splits and cash dividends, ex-dated. Append-only.
--
-- A correction NEVER edits a row. It inserts the corrected row and marks the
-- old one superseded. The active set is superseded_by IS NULL, which is what
-- corporate_actions_active exposes.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS corporate_actions (
    id            bigserial     PRIMARY KEY,
    ticker        text          NOT NULL,
    ex_date       date          NOT NULL,
    action_type   text          NOT NULL,
    -- Exactly one of these is populated, enforced below.
    split_ratio   numeric(18,8),
    cash_dividend numeric(18,8),
    -- Carried for the reconciliation harness, never trusted as input: whether
    -- adjDividend is already split-adjusted is an open question until the spike
    -- answers it.
    provider_adj_dividend numeric(18,8),
    source        text          NOT NULL,
    observed_at   timestamptz   NOT NULL DEFAULT now(),
    superseded_by bigint        REFERENCES corporate_actions(id),
    superseded_at timestamptz,

    CONSTRAINT corporate_actions_type
        CHECK (action_type IN ('split', 'cash_dividend')),
    CONSTRAINT corporate_actions_split_shape
        CHECK (action_type <> 'split'
               OR (split_ratio IS NOT NULL AND split_ratio > 0 AND cash_dividend IS NULL)),
    CONSTRAINT corporate_actions_dividend_shape
        CHECK (action_type <> 'cash_dividend'
               OR (cash_dividend IS NOT NULL AND cash_dividend > 0 AND split_ratio IS NULL)),
    -- superseded_at is the authoritative "no longer active" marker and is set
    -- FIRST; superseded_by is filled in once the replacement row exists. So the
    -- implication runs one way only: a row that names its replacement must
    -- already be marked superseded, but not vice versa.
    CONSTRAINT corporate_actions_supersede_order
        CHECK (superseded_by IS NULL OR superseded_at IS NOT NULL),
    CONSTRAINT corporate_actions_no_self_supersede
        CHECK (superseded_by IS DISTINCT FROM id)
);

-- At most one ACTIVE action of a given type per ticker-date. Superseded rows
-- are exempt, which is what lets a correction sit alongside what it replaces.
--
-- Keyed on superseded_at, NOT on superseded_by, and the reason is load-bearing:
-- a partial unique index cannot be deferred, so if "active" meant
-- "superseded_by IS NULL" the correction would deadlock against itself - the
-- replacement cannot be inserted while the old row is still active, and the old
-- row cannot point at a replacement that does not exist yet. Marking
-- superseded_at first breaks the cycle. See write_actions in repository.py.
CREATE UNIQUE INDEX IF NOT EXISTS corporate_actions_active_unique
    ON corporate_actions (ticker, ex_date, action_type)
    WHERE superseded_at IS NULL;

CREATE INDEX IF NOT EXISTS corporate_actions_ticker_date_idx
    ON corporate_actions (ticker, ex_date);

COMMENT ON TABLE corporate_actions IS
    'Splits and cash dividends, ex-dated. Append-only; corrections supersede.';

-- Append-only, with one exception: marking a row superseded. Anything else is
-- refused, so a "quick fix" to a ratio cannot silently rewrite history.
CREATE OR REPLACE FUNCTION corporate_actions_append_only() RETURNS trigger AS $$
BEGIN
    IF (NEW.ticker, NEW.ex_date, NEW.action_type, NEW.split_ratio,
        NEW.cash_dividend, NEW.provider_adj_dividend, NEW.source, NEW.observed_at)
       IS DISTINCT FROM
       (OLD.ticker, OLD.ex_date, OLD.action_type, OLD.split_ratio,
        OLD.cash_dividend, OLD.provider_adj_dividend, OLD.source, OLD.observed_at)
    THEN
        RAISE EXCEPTION
            'corporate_actions is append-only: attempted to edit %/% (%). Insert a corrected row and set superseded_by instead.',
            OLD.ticker, OLD.ex_date, OLD.action_type;
    END IF;
    IF OLD.superseded_by IS NOT NULL AND NEW.superseded_by IS DISTINCT FROM OLD.superseded_by THEN
        RAISE EXCEPTION
            'corporate_actions row % is already superseded by %; supersede the replacement instead.',
            OLD.id, OLD.superseded_by;
    END IF;
    IF OLD.superseded_at IS NOT NULL AND NEW.superseded_at IS NULL THEN
        RAISE EXCEPTION
            'corporate_actions row % cannot be un-superseded; insert a further correction instead.',
            OLD.id;
    END IF;
    RETURN NEW;
END;
$$ LANGUAGE plpgsql;

DROP TRIGGER IF EXISTS corporate_actions_append_only_trg ON corporate_actions;
CREATE TRIGGER corporate_actions_append_only_trg
    BEFORE UPDATE ON corporate_actions
    FOR EACH ROW EXECUTE FUNCTION corporate_actions_append_only();

CREATE OR REPLACE VIEW corporate_actions_active AS
    SELECT id, ticker, ex_date, action_type, split_ratio, cash_dividend,
           provider_adj_dividend, source, observed_at
      FROM corporate_actions
     WHERE superseded_at IS NULL;

-- ---------------------------------------------------------------------------
-- quarantine: rows that failed a SOFT validation rule, with the rule that fired.
--
-- Two tiers, per the README. Hard rejects never reach storage at all - they are
-- CHECK constraints on daily_bars and pandera failures at the provider boundary.
-- Soft failures land here: the bar is real and stored, but flagged, surfaced in
-- the API response, and never silently propagated into a backtest.
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS quarantine (
    id             bigserial   PRIMARY KEY,
    ticker         text        NOT NULL,
    bar_date       date        NOT NULL,
    rule           text        NOT NULL,
    detail         text        NOT NULL,
    observed_value numeric(18,8),
    threshold      numeric(18,8),
    flagged_at     timestamptz NOT NULL DEFAULT now(),

    CONSTRAINT quarantine_rule_known CHECK (rule IN (
        'abs_log_return',      -- generic outlier
        'split_shaped_move',   -- move matches a common split ratio, no action on file
        'calendar_gap',        -- gap the trading calendar does not explain
        'action_coincidence'   -- a split and a dividend share an ex-date: the one
                               -- case the dividend-unit spike could not test
    ))
);

-- One row per (ticker, date, rule). Re-running validation is a no-op, not a
-- duplicate, which keeps the backfill job idempotent here too.
CREATE UNIQUE INDEX IF NOT EXISTS quarantine_unique
    ON quarantine (ticker, bar_date, rule);

CREATE INDEX IF NOT EXISTS quarantine_ticker_date_idx
    ON quarantine (ticker, bar_date);

COMMENT ON TABLE quarantine IS
    'Bars that failed a soft validation rule. Stored, flagged, surfaced - never dropped.';

COMMIT;
