from alphaforge.core.statistics import safe_div
from alphaforge.core.valuation.raw_valuation import RawValuation
from alphaforge.core.valuation.types import CurrentValuation, HistoricalValuation, ValuationResult


class ValuationCalculator:
    def calculate(
        self,
        current: CurrentValuation,
        historical: HistoricalValuation,
        raw: RawValuation | None = None,
    ) -> ValuationResult:

        return ValuationResult(
            pe=current.pe,
            ev_ebit=current.ev_ebit,
            ev_ebitda=current.ev_ebitda,
            pb=current.pb,
            ps=current.ps,
            pfcf=current.pfcf,
            peg=current.peg,
            earnings_yield=self.calculate_earnings_yield(current),
            free_cash_flow_yield=self.calculate_fcf_yield(current),
            pe_vs_5y_avg=self.calculate_pe(current, historical),
            ev_ebit_vs_5y_avg=self.calculate_ev_ebit(current, historical),
            pb_vs_5y_avg=self.calculate_pb(current, historical),
            pe_percentile=self.calculate_pe_percentile(current, historical),
            ev_ebit_percentile=self.calculate_ev_ebit_percentile(current, historical),
            ev_ebit_guardrail_low=self.calculate_history_bound(historical.ev_ebit_history, 0.10),
            ev_ebit_guardrail_high=self.calculate_history_bound(historical.ev_ebit_history, 0.90),
            ev_ebit_base_ceiling=self.calculate_history_bound(historical.ev_ebit_history, 0.25),
            ev_ebit_bull_ceiling=self.calculate_history_bound(historical.ev_ebit_history, 0.75),
            ev_ebit_history_count=len([value for value in historical.ev_ebit_history if value > 0]),
            raw_market_cap=raw.market_cap if raw else None,
            raw_enterprise_value=raw.enterprise_value if raw else None,
            raw_earnings_yield=raw.earnings_yield if raw else None,
            raw_fcf_yield=raw.fcf_yield if raw else None,
            raw_pe=raw.pe if raw else None,
            raw_pfcf=raw.pfcf if raw else None,
            raw_ev_ebit=raw.ev_ebit if raw else None,
            raw_ev_ebitda=raw.ev_ebitda if raw else None,
            dividend_yield=current.dividend_yield,
        )

    def calculate_earnings_yield(self, current):
        return self.calculate_ratio(1, current.pe)

    def calculate_fcf_yield(self, current):
        return self.calculate_ratio(1, current.pfcf)

    def calculate_pe(self, current, historical):
        return self.calculate_ratio(current.pe, historical.avg_pe)

    def calculate_ev_ebit(self, current, historical):
        return self.calculate_ratio(current.ev_ebit, historical.avg_ev_ebit)

    def calculate_pb(self, current, historical):
        return self.calculate_ratio(current.pb, historical.avg_pb)

    def calculate_pe_percentile(self, current, historical):
        return self.calculate_percentile(
            current.pe,
            historical.pe_history,
        )

    def calculate_ev_ebit_percentile(self, current, historical):
        return self.calculate_percentile(
            current.ev_ebit,
            historical.ev_ebit_history,
        )

    # Internal helpers
    def calculate_percentile(
        self,
        value: float | None,
        history: list[float],
    ) -> float | None:

        if value is None or not history:
            return None

        values_below = sum(1 for x in history if x <= value)

        return values_below / len(history) * 100

    def calculate_ratio(self, value: float | None, denominator: float | None) -> float | None:
        return safe_div(value, denominator)

    @staticmethod
    def calculate_history_bound(history: list[float], percentile: float) -> float | None:
        values = sorted(value for value in history if value > 0)
        if len(values) < 5:
            return None
        position = (len(values) - 1) * percentile
        lower_index = int(position)
        upper_index = min(lower_index + 1, len(values) - 1)
        fraction = position - lower_index
        return values[lower_index] + (values[upper_index] - values[lower_index]) * fraction
