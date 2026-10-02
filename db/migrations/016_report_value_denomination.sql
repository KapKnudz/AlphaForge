-- Record the denomination and requested conversion contract at report acquisition.
-- Existing rows intentionally remain NULL: mutable company metadata cannot prove
-- their historical values currency, conversion mode, or target.
ALTER TABLE financial_periods ADD COLUMN values_currency TEXT;
ALTER TABLE financial_periods ADD COLUMN conversion_mode TEXT;
ALTER TABLE financial_periods ADD COLUMN conversion_target_currency TEXT;
