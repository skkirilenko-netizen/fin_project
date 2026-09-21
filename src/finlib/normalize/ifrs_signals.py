"""Справочник надзорных сигналов ветки МСФО.

**Параллельный справочник, а не продолжение справочника РСБУ** — по той же
причине, по какой параллельны справочники статей и показателей: величины
опознаются кодами позиций `ifrs.*`, отсечки привязаны к валюте баланса,
а не к выручке, и состав признаков свой. Арифметика при этом одна
(`scoring/signals.py`): расходиться должны входы, а не правило.

**Непереносимый признак объявляется вместе с причиной.** Перечень признаков
ветки сравнивают с перечнем РСБУ, и отсутствующий признак без причины
неотличим от забытого. Признак, который на консолидированной отчётности
не срабатывает никогда, хуже отсутствующего: ноль срабатываний неотличим
от невыполненного контроля.
"""

import logging
from functools import lru_cache
from pathlib import Path
from typing import Self

import yaml
from pydantic import BaseModel, ConfigDict, Field, model_validator

from finlib.config import settings
from finlib.scoring.signals import SignalsCatalog

logger = logging.getLogger(__name__)


class NotTransferred(BaseModel):
    """Признак РСБУ, который в ветку МСФО не переносится, и почему."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str = Field(min_length=1)
    name: str = Field(min_length=1)
    reason: str = Field(min_length=1)


class IfrsSignalsCatalog(SignalsCatalog):
    """Справочник сигналов ветки вместе с перечнем непереносимых признаков."""

    not_transferred: tuple[NotTransferred, ...] = ()

    @model_validator(mode="after")
    def _transferred_and_refused_do_not_overlap(self) -> Self:
        """Признак либо перенесён, либо объявлен непереносимым — не оба сразу."""
        declared = {item.code for item in self.signals}
        clashing = sorted(declared & {item.code for item in self.not_transferred})
        if clashing:
            raise ValueError(
                f"признаки объявлены и перенесёнными, и непереносимыми: {clashing}"
            )
        return self


def default_path() -> Path:
    """Путь к справочнику сигналов ветки МСФО."""
    return settings.methodology_dir / "ifrs_signals.yaml"


@lru_cache(maxsize=1)
def load_ifrs_signals(path: Path | None = None) -> IfrsSignalsCatalog:
    """Читает справочник надзорных сигналов ветки МСФО."""
    target = path or default_path()
    catalog = IfrsSignalsCatalog(
        **yaml.safe_load(target.read_text(encoding="utf-8"))
    )
    active = sum(1 for item in catalog.signals if item.active)
    logger.info(
        "справочник сигналов МСФО %s: признаков %d, из них действующих %d, "
        "непереносимых %d",
        catalog.version,
        len(catalog.signals),
        active,
        len(catalog.not_transferred),
    )
    return catalog
