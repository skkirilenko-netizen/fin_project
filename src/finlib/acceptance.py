"""Приёмка части первой: условия пройденной экспертной проверки (`methodology/acceptance.yaml`)."""

from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path

import yaml

from finlib.config import settings


@dataclass(frozen=True, slots=True)
class ExpertReviewAcceptance:
    """Когда экспертная проверка пройдена и какие проверки идут в счёт."""

    extra_share_max: Decimal
    critical_max: int
    consecutive: int
    counted_from: date


def load_acceptance(path: Path | None = None) -> ExpertReviewAcceptance:
    """Условия приёмки экспертной проверки; величины отсюда только читаются."""
    where = path or settings.methodology_dir / "acceptance.yaml"
    raw = yaml.safe_load(where.read_text(encoding="utf-8"))["expert_review"]
    return ExpertReviewAcceptance(
        extra_share_max=Decimal(str(raw["extra_share_max"])),
        critical_max=int(raw["critical_max"]),
        consecutive=int(raw["consecutive"]),
        counted_from=date.fromisoformat(str(raw["counted_from"])),
    )
