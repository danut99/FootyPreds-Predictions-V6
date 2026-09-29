# Predicții tenis — model compact ATP

Modelul folosește implicit patru fișiere anuale ATP, în ordine cronologică. Combină Elo
general, Elo specific suprafeței și rankingul disponibil înaintea meciului. Nu folosește
scorul sau statisticile meciului prezis.

Benchmark fără leakage:

```powershell
.\.venv\Scripts\python.exe -m tenisPrediction.evaluate --test-year 2025
```

Utilizare cu un meci primit de API-ul existent:

```python
from tenisPrediction import CompactTennisModel

model = CompactTennisModel.train_recent("tenisPrediction/tml-data", end_year=2025)
result = model.predict_api_match(match)  # footypreds.domain.Match
```

Interfața separată poate fi pornită pe portul 8010:

```powershell
.\.venv\Scripts\python.exe -m uvicorn tenisPrediction.app:app --host 127.0.0.1 --port 8010
```

`decision` este `selectează` numai peste pragul implicit 72% și cu minimum cinci meciuri
istorice pentru ambii jucători. În rest răspunsul este `fără pariu`; filtrarea este esențială
pentru precizie și nu garantează profit.
