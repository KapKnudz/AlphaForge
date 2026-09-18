from alphaforge.core.financial.types import CurrentFinancials, HistoricalFinancials
from alphaforge.core.types import Report


class FinancialMapper:
    def to_current(self, report: Report) -> CurrentFinancials:
        return CurrentFinancials(
            revenue=report.revenue,
            operating_profit=report.operating_profit,
            ebit=report.ebit,
            ebitda=report.ebitda,
            net_income=report.net_income,
            free_cash_flow=report.free_cash_flow,
            equity=report.equity,
            total_assets=report.total_assets,
            total_debt=report.total_debt,
            shares_outstanding=report.shares_outstanding,
            gross_income=report.gross_income,
            operating_cash_flow=report.operating_cash_flow,
        )

    def to_historical(self, reports: list[Report]) -> HistoricalFinancials:
        return HistoricalFinancials(
            revenue_history=[r.revenue for r in reports],
            ebit_history=[r.ebit for r in reports],
            fcf_history=[r.free_cash_flow for r in reports],
            net_income_history=[r.net_income for r in reports],
            equity_history=[r.equity for r in reports],
            shares_history=[r.shares_outstanding for r in reports],
            operating_profit_history=[r.operating_profit for r in reports],
            operating_cash_flow_history=[r.operating_cash_flow for r in reports],
        )
