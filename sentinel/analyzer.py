"""Классификация новостей по уровням угрозы."""

from enum import IntEnum


class ThreatLevel(IntEnum):
    NONE = 0
    WATCH = 1
    CAUTION = 2
    DANGER = 3


class NewsAnalyzer:
    def __init__(self, keywords: dict):
        self._kw = keywords

    def analyze(self, headline: str) -> tuple[ThreatLevel, str]:
        """Возвращает (уровень_угрозы, совпавшее_ключевое_слово)."""
        text = headline.lower()

        # Проверяем от наивысшего уровня вниз
        for kw in self._kw.get("level_3", {}).get("en", []):
            if kw.lower() in text:
                return ThreatLevel.DANGER, kw
        for kw in self._kw.get("level_3", {}).get("ru", []):
            if kw.lower() in text:
                return ThreatLevel.DANGER, kw

        # Уровень 2: биржа + ограничение + Россия
        exchange_names = [e.lower() for e in self._kw.get("exchange_names", [])]
        has_exchange = any(ex in text for ex in exchange_names)

        for kw in self._kw.get("level_2", {}).get("en", []):
            if kw.lower() in text:
                return ThreatLevel.CAUTION, kw
        for kw in self._kw.get("level_2", {}).get("ru", []):
            if kw.lower() in text:
                return ThreatLevel.CAUTION, kw

        # Уровень 2 можно получить комбинацией: exchange + level_1 keywords
        if has_exchange:
            for kw in self._kw.get("level_1", {}).get("en", []):
                if kw.lower() in text:
                    return ThreatLevel.CAUTION, f"{kw} (+ exchange name)"

        for kw in self._kw.get("level_1", {}).get("en", []):
            if kw.lower() in text:
                return ThreatLevel.WATCH, kw
        for kw in self._kw.get("level_1", {}).get("ru", []):
            if kw.lower() in text:
                return ThreatLevel.WATCH, kw

        return ThreatLevel.NONE, ""
