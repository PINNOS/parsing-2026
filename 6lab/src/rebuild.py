"""
Пересборка clean-слоя из raw-слоя (без обращения к сети).

Исполняемая реализация конвейера `raw -> clean` — в ноутбуке `6lab.ipynb`
(раздел «Пересборка clean из raw»): шесть шагов очистки, бизнес-ключ,
контракт Pydantic и проверка совпадения множества ключей с текущим clean.
Модуль `src/clean.py` держит имена шагов протокола, `src/contract.py` — модель.

Здесь оставлен только независимый доступ к сырому слою, чтобы пересборку
можно было запустить и из командной строки, не открывая ноутбук.
"""

from pathlib import Path

import pandas as pd

RAW_DIR = Path(__file__).resolve().parent.parent / "raw"
CLEAN_DIR = Path(__file__).resolve().parent.parent / "clean"


def load_raw() -> pd.DataFrame:
    """Читает ВСЕ файлы raw/*.jsonl как единый кадр (сырой слой append-only)."""
    frames = []
    for p in sorted(RAW_DIR.glob("*.jsonl")):
        try:
            frames.append(pd.read_json(p, lines=True))
        except ValueError:
            continue
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


if __name__ == "__main__":
    raw = load_raw()
    print(f"raw: {len(raw)} записей из {len(list(RAW_DIR.glob('*.jsonl')))} файлов")
    print("Полный конвейер очистки и пересборку clean выполняет 6lab.ipynb")
