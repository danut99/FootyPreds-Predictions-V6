# Migrare Excel → Python

## Fișiere păstrate

- `FootyPreds.rar` → `legacy/FootyPreds.rar`.
- `V6 predictions.xlsm` → `legacy/V6 predictions.xlsm`.
- Extragerea `FootyPreds/` → `legacy/local/FootyPreds/`, inclusiv VBA, JSON și variantele vechi.
- README original → `legacy/README-V6.md`.
- `AGENTS.md` a rămas nemodificat.

Nu au fost șterse date din versiunile vechi. Arhivele binare și extragerea sunt copii locale excluse de `.gitignore`: conțin sau pot conține credențiale istorice. Prin urmare Git poate raporta dispariția fișierelor binare din vechile căi; copiile sunt în `legacy/`.

## Înlocuiri

| V6 | V7 |
|---|---|
| Worksheet MAIN / butoane VBA | Dashboard HTML |
| Căutare manuală în textul JSON | Decodare JSON și validare Pydantic |
| Chei API în module | `.env` local, exclus din Git |
| Cereri repetate, rotație chei | Cache SQLite, cereri serializate, erori explicite pentru limite |
| Calibrare cu constante manuale | Model declarat necalibrat, evaluare măsurabilă |
| Rezultate introduse manual | Jurnal pre-match și decontare din FlashScore |
| Statistici fără protocol temporal | Backtest walk-forward cu excluderea informațiilor viitoare |

Nu s-au copiat arbitrar coeficienții V6 sau seturile xG fără dată. Predicțiile live și cornerele nu sunt portate. Biletele au o implementare nouă: țintă de cotă, maximum de selecții, calendar de 7 zile și salvare SQLite, fără plasare sau mize automate. XLSM nu este încărcat sau executat de Python. Un istoric exportat poate fi evaluat folosind formatul CSV documentat în README.
