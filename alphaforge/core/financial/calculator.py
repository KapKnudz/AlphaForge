from statistics import pstdev

from alphaforge.core.financial.types import CurrentFinancials, FinancialResult, HistoricalFinancials
from alphaforge.core.statistics import safe_div


class FinancialCalculator:
    def calculate(
        self,
        current: CurrentFinancials,
        historical: HistoricalFinancials,
        growth_current: CurrentFinancials | None = None,
        latest_quarter: CurrentFinancials | None = None,
        prior_year_quarter: CurrentFinancials | None = None,
    ) -> FinancialResult:

        growth_current = growth_current or current

        (
            revenue_growth,
            revenue_years,
            ebit_growth,
            ebit_years,
            net_income_growth,
            net_income_years,
            one_off_risk,
        ) = self._growth_metrics(growth_current, historical)
        (
            revenue_per_share_growth,
            ebit_per_share_growth,
            net_income_per_share_growth,
            fcf_per_share_growth,
            book_value_per_share_growth,
            per_share_years,
            share_count_growth,
        ) = self._per_share_metrics(growth_current, historical)
        (
            margin_volatility,
            positive_fcf_ratio,
            recent_revenue_growth,
            recent_growth_acceleration,
            recent_growth_slowdown,
        ) = self._quality_metrics(
            growth_current, historical, latest_quarter, prior_year_quarter, revenue_growth
        )

        if current.net_debt is not None:
            effective_net_debt = current.net_debt
        elif current.total_debt is not None and current.cash is not None:
            effective_net_debt = current.total_debt - current.cash
        else:
            effective_net_debt = None
        return FinancialResult(
            operating_margin=self.calculate_ratio(current.operating_profit, current.revenue),
            net_margin=self.calculate_ratio(current.net_income, current.revenue),
            fcf_margin=self.calculate_ratio(current.free_cash_flow, current.revenue),
            revenue_growth=revenue_growth,
            ebit_growth=ebit_growth,
            net_income_growth=net_income_growth,
            roe=self.calculate_ratio(current.net_income, current.equity),
            roa=self.calculate_ratio(current.net_income, current.total_assets),
            debt_to_equity=self.calculate_ratio(current.total_debt, current.equity),
            net_debt=effective_net_debt,
            equity=current.equity,
            revenue_growth_years=revenue_years,
            ebit_growth_years=ebit_years,
            net_income_growth_years=net_income_years,
            revenue_turnaround=self.is_turnaround(
                growth_current.revenue,
                historical.revenue_history,
            ),
            ebit_turnaround=self.is_turnaround(growth_current.ebit, historical.ebit_history),
            net_income_turnaround=self.is_turnaround(
                growth_current.net_income,
                historical.net_income_history,
            ),
            revenue_deterioration=self.is_deterioration(
                growth_current.revenue,
                historical.revenue_history,
            ),
            ebit_deterioration=self.is_deterioration(
                growth_current.ebit,
                historical.ebit_history,
            ),
            net_income_deterioration=self.is_deterioration(
                growth_current.net_income,
                historical.net_income_history,
            ),
            earnings_growth_one_off_risk=one_off_risk,
            revenue_per_share_growth=revenue_per_share_growth,
            ebit_per_share_growth=ebit_per_share_growth,
            net_income_per_share_growth=net_income_per_share_growth,
            fcf_per_share_growth=fcf_per_share_growth,
            book_value_per_share_growth=book_value_per_share_growth,
            share_count_growth=share_count_growth,
            per_share_growth_years=per_share_years,
            share_dilution=bool(share_count_growth is not None and share_count_growth > 0.05),
            gross_margin=self.calculate_ratio(current.gross_income, current.revenue),
            cash_conversion=(
                self.calculate_ratio(current.operating_cash_flow, current.net_income)
                if current.net_income is not None and current.net_income > 0
                else None
            ),
            fcf_conversion=(
                self.calculate_ratio(current.free_cash_flow, current.net_income)
                if current.net_income is not None and current.net_income > 0
                else None
            ),
            operating_margin_volatility=margin_volatility,
            positive_fcf_ratio=positive_fcf_ratio,
            recent_revenue_growth=recent_revenue_growth,
            recent_growth_acceleration=recent_growth_acceleration,
            recent_growth_slowdown=recent_growth_slowdown,
        )

    def _growth_metrics(self, current, historical):
        growths = [
            self._growth(current.revenue, historical.revenue_history),
            self._growth(current.ebit, historical.ebit_history),
            self._growth(current.net_income, historical.net_income_history),
        ]
        yoy = [
            self.calculate_yoy_change(current.revenue, historical.revenue_history),
            self.calculate_yoy_change(current.ebit, historical.ebit_history),
            self.calculate_yoy_change(current.net_income, historical.net_income_history),
        ]
        return (
            growths[0][0],
            growths[0][1],
            growths[1][0],
            growths[1][1],
            growths[2][0],
            growths[2][1],
            self.has_earnings_one_off_risk(*yoy),
        )

    def _per_share_metrics(self, current, historical):
        histories = {
            name: self._per_share_values(values, historical.shares_history)
            for name, values in {
                "revenue": historical.revenue_history,
                "ebit": historical.ebit_history,
                "net_income": historical.net_income_history,
                "fcf": historical.fcf_history,
                "equity": historical.equity_history,
            }.items()
        }
        values = {
            "revenue": current.revenue,
            "ebit": current.ebit,
            "net_income": current.net_income,
            "fcf": current.free_cash_flow,
            "equity": current.equity,
        }
        growths = [
            self._growth(self.calculate_ratio(value, current.shares_outstanding), histories[name])
            for name, value in values.items()
        ]
        share_growth = self._growth(current.shares_outstanding, historical.shares_history)
        return (*[item[0] for item in growths], growths[0][1], share_growth[0])

    def _quality_metrics(self, current, historical, latest_quarter, prior_year_quarter, growth):
        recent = (
            self.calculate_pair_change(latest_quarter.revenue, prior_year_quarter.revenue)
            if latest_quarter is not None and prior_year_quarter is not None
            else None
        )
        margins = self._margin_history(
            current.operating_profit,
            current.revenue,
            historical.operating_profit_history,
            historical.revenue_history,
        )
        fcf_values = [
            value
            for value in historical.fcf_history + [current.free_cash_flow]
            if value is not None
        ]
        positive_fcf_ratio = (
            sum(value > 0 for value in fcf_values) / len(fcf_values) if fcf_values else None
        )
        return (
            pstdev(margins) if len(margins) >= 2 else None,
            positive_fcf_ratio,
            recent,
            bool(recent is not None and growth is not None and recent >= growth + 0.10),
            bool(recent is not None and growth is not None and recent <= growth - 0.10),
        )

    def calculate_growth(
        self, current_value: float | None, history: list[float | None]
    ) -> float | None:
        return self._growth(current_value, history)[0]

    def _growth(
        self,
        current_value: float | None,
        history: list[float | None],
    ) -> tuple[float | None, int]:
        if current_value is None or current_value <= 0 or not history:
            return None, 1

        previous_value = history[-1]
        if previous_value is None or previous_value <= 0:
            return None, 1

        periods = min(3, len(history))
        baseline = history[-periods]
        if baseline is None or baseline <= 0:
            periods = 1
            baseline = previous_value

        return (current_value / baseline) ** (1 / periods) - 1, periods

    @staticmethod
    def calculate_yoy_change(
        current_value: float | None,
        history: list[float | None],
    ) -> float | None:
        if current_value is None or not history or history[-1] in (None, 0):
            return None
        previous_value = history[-1]
        return (current_value - previous_value) / abs(previous_value)

    @staticmethod
    def calculate_pair_change(
        current_value: float | None,
        previous_value: float | None,
    ) -> float | None:
        if current_value is None or previous_value in (None, 0):
            return None
        return (current_value - previous_value) / abs(previous_value)

    def _per_share_values(
        self,
        values: list[float | None],
        shares: list[float | None],
    ) -> list[float | None]:
        return [
            self.calculate_ratio(value, share_count)
            for value, share_count in zip(values, shares, strict=False)
        ]

    def _margin_history(
        self,
        current_profit: float | None,
        current_revenue: float | None,
        profit_history: list[float | None],
        revenue_history: list[float | None],
    ) -> list[float]:
        values = [
            self.calculate_ratio(profit, revenue)
            for profit, revenue in zip(profit_history, revenue_history, strict=False)
        ]
        values.append(self.calculate_ratio(current_profit, current_revenue))
        return [value for value in values[-5:] if value is not None]

    @staticmethod
    def is_turnaround(current_value: float | None, history: list[float | None]) -> bool:
        return bool(
            current_value is not None
            and current_value > 0
            and history
            and history[-1] is not None
            and history[-1] <= 0
        )

    @staticmethod
    def is_deterioration(current_value: float | None, history: list[float | None]) -> bool:
        return bool(
            current_value is not None
            and current_value <= 0
            and history
            and history[-1] is not None
            and history[-1] > 0
        )

    @staticmethod
    def has_earnings_one_off_risk(
        revenue_yoy: float | None,
        ebit_yoy: float | None,
        net_income_yoy: float | None,
    ) -> bool:
        if revenue_yoy is None:
            return False
        threshold = max(0.75, max(revenue_yoy, 0) * 2)
        return any(
            growth is not None and growth > threshold for growth in (ebit_yoy, net_income_yoy)
        )

    def calculate_ratio(self, numerator: float | None, denominator: float | None) -> float | None:
        return safe_div(numerator, denominator)
