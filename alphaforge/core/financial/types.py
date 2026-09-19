from dataclasses import dataclass, field


@dataclass
class CurrentFinancials:
    revenue: float | None
    operating_profit: float | None
    ebit: float | None
    ebitda: float | None
    net_income: float | None
    free_cash_flow: float | None
    equity: float | None
    total_assets: float | None
    total_debt: float | None
    shares_outstanding: float | None = None
    gross_income: float | None = None
    operating_cash_flow: float | None = None


@dataclass
class HistoricalFinancials:
    revenue_history: list[float | None]
    ebit_history: list[float | None]
    fcf_history: list[float | None]
    net_income_history: list[float | None]
    equity_history: list[float | None] = field(default_factory=list)
    shares_history: list[float | None] = field(default_factory=list)
    operating_profit_history: list[float | None] = field(default_factory=list)
    operating_cash_flow_history: list[float | None] = field(default_factory=list)


@dataclass
class FinancialResult:
    # Margins
    operating_margin: float | None
    net_margin: float | None
    fcf_margin: float | None

    # Growth (Year-over-Year)
    revenue_growth: float | None
    ebit_growth: float | None
    net_income_growth: float | None

    # Returns
    roe: float | None
    roa: float | None

    # Health
    debt_to_equity: float | None

    # Inputs retained so net cash can be distinguished from negative equity.
    net_debt: float | None = None
    equity: float | None = None

    # Growth context. Growth rates use up to a three-year CAGR.
    revenue_growth_years: int = 1
    ebit_growth_years: int = 1
    net_income_growth_years: int = 1
    revenue_turnaround: bool = False
    ebit_turnaround: bool = False
    net_income_turnaround: bool = False
    revenue_deterioration: bool = False
    ebit_deterioration: bool = False
    net_income_deterioration: bool = False
    earnings_growth_one_off_risk: bool = False

    # Per-share growth protects the ranking from acquisition growth funded by dilution.
    revenue_per_share_growth: float | None = None
    ebit_per_share_growth: float | None = None
    net_income_per_share_growth: float | None = None
    fcf_per_share_growth: float | None = None
    book_value_per_share_growth: float | None = None
    share_count_growth: float | None = None
    per_share_growth_years: int = 1
    share_dilution: bool = False

    # Current cash quality and multi-year consistency.
    gross_margin: float | None = None
    cash_conversion: float | None = None
    fcf_conversion: float | None = None
    operating_margin_volatility: float | None = None
    positive_fcf_ratio: float | None = None

    # Latest quarter compared with the corresponding prior-year quarter.
    recent_revenue_growth: float | None = None
    recent_growth_acceleration: bool = False
    recent_growth_slowdown: bool = False
