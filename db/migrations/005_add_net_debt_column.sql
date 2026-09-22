-- Add dedicated net_debt column so provider net_Debt is not mislabelled as gross total_Debt.
-- Live Börsdata returns net_Debt (already net of cash) and total_Equity, not book_Value/total_Debt.
-- Keep total_debt for backward compatibility; new column stores net_Debt verbatim.
ALTER TABLE financial_periods ADD COLUMN net_debt REAL;
