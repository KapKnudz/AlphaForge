from collections.abc import Callable
from dataclasses import dataclass, field


@dataclass(frozen=True)
class MetricResult:
    name: str
    raw_value: float | None
    score: float | None
    weight: float
    transformation: str
    dependencies: tuple[str, ...] = ()
    cross_category_dependencies: tuple[str, ...] = ()
    positives: list[str] = field(default_factory=list)
    negatives: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    missing: list[str] = field(default_factory=list)
    provenance: str = "report_reconstructed"


class Metric:
    def __init__(
        self,
        name: str,
        calculate_raw: Callable[..., float | None],
        calculate_score: Callable[[float | None], float | None],
        weight: float = 1.0,
        transformation: str = "",
        dependencies: tuple[str, ...] = (),
        cross_category_dependencies: tuple[str, ...] = (),
        describe: Callable[[float | None], tuple[list[str], list[str], list[str]]] | None = None,
    ):
        self.name = name
        self.calculate_raw = calculate_raw
        self.calculate_score = calculate_score
        self.weight = weight
        self.transformation = transformation
        self.dependencies = dependencies
        self.cross_category_dependencies = cross_category_dependencies
        self.describe = describe

    def evaluate(self, *args, **kwargs) -> MetricResult:
        raw_value = self.calculate_raw(*args, **kwargs)
        score = self.calculate_score(raw_value)

        positives, negatives, flags = [], [], []
        missing = []
        if raw_value is None:
            missing.append(f"{self.name} not available")
        elif self.describe:
            positives, negatives, flags = self.describe(raw_value)

        return MetricResult(
            name=self.name,
            raw_value=raw_value,
            score=score,
            weight=self.weight,
            transformation=self.transformation,
            dependencies=self.dependencies,
            cross_category_dependencies=self.cross_category_dependencies,
            positives=positives,
            negatives=negatives,
            flags=flags,
            missing=missing,
        )
