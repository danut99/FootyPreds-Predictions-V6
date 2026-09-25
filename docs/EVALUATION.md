# Protocol de evaluare strictă

## Date și reproducere

Surse: [Football-Data](https://www.football-data.co.uk/data.php), [descrierea coloanelor](https://www.football-data.co.uk/notes.txt). Descărcătorul folosește 20 de CSV-uri: sezoanele 2021–2022, 2022–2023, 2023–2024 și 2024–2025 pentru Premier League, La Liga, Bundesliga, Serie A și Ligue 1.

Fișierele brute sunt păstrate local, împreună cu URL-ul, data descărcării, numărul de meciuri și SHA256. Datasetul normalizat are propriul checksum, verificat la fiecare evaluare. Descărcarea și evaluarea nu scriu în jurnalul live sau în planurile de bilete.

```powershell
.\.venv\Scripts\python.exe -m evaluation.dataset
.\.venv\Scripts\python.exe -m evaluation.run
.\.venv\Scripts\python.exe -m pytest tests/test_evaluation.py tests/test_model.py -q
```

`evaluation/protocol.json` fixează ligile, sezoanele, pragul 0.85, istoricul bogat (minimum 20 de observații per echipă) și seed-ul bootstrap înainte de test. Raportul include hash-ul protocolului și al sursei modelului. Nu schimba protocolul pentru a cosmetiza rezultatul.

## Separarea informației

1. Primele trei sezoane constituie istoricul inițial. Modelul Poisson estimează rate din rezultate, nu antrenează un clasificator supravegheat.
2. Ultimul sezon este parcurs cronologic, pe zile. La fiecare zi sunt disponibile numai rezultatele zilelor precedente; rezultatele deja trecute din sezonul de test pot intra ulterior în istoric, ca într-o utilizare reală.
3. Obiectul `BlindFixture` primit de model **nu are câmpuri de scor final**. Nu conține statistici live și oferă un dicționar de cote gol, nemodificabil.
4. Se fac toate predicțiile zilei. Abia după aceea, evaluatorul citește scorurile și actualizează istoricul pentru ziua următoare.
5. Ora din CSV nu este folosită: toate meciurile primesc începutul zilei UTC. Excluderea întregii zile este conservatoare și evită folosirea unui rezultat simultan sau cu oră ambiguă.

Un test care încearcă să citească `home_goals` din `BlindFixture` primește `AttributeError`. Alte teste verifică permutarea ordinii de intrare, scoruri viitoare modificate la extreme, meciuri simultane, duplicate cu același ID sau identitate și suprapunerea perioadelor. Testele de unitate nu cer o acuratețe arbitrară: verifică lipsa contaminării și corectitudinea calculului.

## Ce măsurăm

- **1X2 pe toate meciurile**: acuratețe, log-loss și Brier multiclasă împărțit la 3. Refuzarea unei selecții nu elimină meciul din acest raport.
- **Repere**: frecvențele istorice ale ligii, alegerea constantă a gazdelor și probabilitățile normalizate din cotele medii. Compararea cu cotele folosește același subset pentru ambele modele. Cotele sunt numai referință externă, cu moment exact de capturare nespecificat.
- **Selecții**: maximum una per meci, piața cu probabilitatea maximă, prag fix 85%, fără micșorarea pragului după rezultat. Raportăm reușite, volum și procentul meciurilor selectate.
- **Incertitudine**: Wilson 95% și 1.000 de resamplări bootstrap pe grupuri de meciuri din aceeași zi, seed 7. Gruparea pe zile nu elimină toate corelațiile între echipe sau sezoane.
- **Diagnostic**: ligi separate, subset cu istoric bogat, Brier și calibrare pe fiecare piață. Ratele pe piețe nu sunt însumate ca și cum fiecare ar fi un meci independent.

## Interpretare și limite

Raportul inițial este în [BENCHMARK.md](BENCHMARK.md). Selecțiile au depășit 85% pe acest holdout, dar modelul 1X2 a rămas sub reperul cotelor. Bundesliga a avut 84,3% pentru selecțiile sale; o medie bună nu înseamnă că toate ligile ating ținta.

Acest test validează selecții individuale, **nu biletele custom sau planurile de șapte zile**. Pentru ele este necesar un benchmark separat cu oferte istorice verificabile pentru toate selecțiile și cu politica de generare fixată înainte de evaluare. Nu deducem profit sau randament din acuratețe.

După consultarea raportului, sezonul 2024–2025 nu mai este un test neatins pentru experimente noi. Orice model CatBoost/LightGBM trebuie să folosească o perioadă separată pentru calibrare și un sezon ulterior, neconsultat, pentru decizia finală. Nu au fost antrenate aceste modele în V7.
