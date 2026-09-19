from alphaforge.core.valuation.types import CurrentValuation, HistoricalValuation


class ValuationMapper:
    def to_current(self, valuation) -> CurrentValuation:
        return CurrentValuation(
            market_cap=valuation.market_cap,
            enterprise_value=valuation.enterprise_value,
            pe=valuation.pe,
            ev_ebit=valuation.ev_ebit,
            ev_ebitda=valuation.ev_ebitda,
            pb=valuation.pb,
            ps=valuation.ps,
            pfcf=valuation.pfcf,
            peg=valuation.peg,
            dividend_yield=valuation.dividend_yield,
        )

    def to_historical(
        self,
        pe_history: list[float],
        ev_ebit_history: list[float],
        pb_history: list[float],
    ) -> HistoricalValuation:

        def safe_avg(values: list[float]) -> float | None:
            return sum(values) / len(values) if values else None

        def safe_median(values: list[float]) -> float | None:
            if not values:
                return None
            return sorted(values)[len(values) // 2]

        return HistoricalValuation(
            pe_history=pe_history,
            ev_ebit_history=ev_ebit_history,
            pb_history=pb_history,
            avg_pe=safe_avg(pe_history),
            avg_ev_ebit=safe_avg(ev_ebit_history),
            avg_pb=safe_avg(pb_history),
            median_pe=safe_median(pe_history),
            median_ev_ebit=safe_median(ev_ebit_history),
            median_pb=safe_median(pb_history),
        )
