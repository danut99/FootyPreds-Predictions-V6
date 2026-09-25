# Direcția produsului V7

O singură aplicație web pentru analiza zilnică a meciurilor, cu motor Python,
stocare SQLite și interfață HTML/CSS/JavaScript. Excel rămâne o opțiune viitoare
de import/export; runtime-ul actual funcționează independent de Excel.

## Module și stadiu

| Modul | Experiența urmărită | Stadiul actual |
| --- | --- | --- |
| Daily Tips | Tabel zilnic, filtre de competiții, probabilități 1X2, BTTS și goluri | Analize individuale și în lot în Meciuri; tabloul agregat urmează |
| Goals Lab | Over/Under 1.5, 2.5, 3.5, goluri așteptate, frecvențe istorice | Probabilitățile există în model; tabelul dedicat și frecvențele urmează |
| Correct Score | Cele mai probabile trei scoruri, fiecare cu probabilitatea lui | Distribuție Poisson existentă; secțiunea dedicată urmează |
| Ticket Lab | Plan de 7 zile, cotă țintă, custom, selecție multiplă de competiții | Implementat; biletele reale folosesc oferta 1X2 disponibilă |
| Results & Backtesting | Predicții păstrate înainte de start, rezultate și evaluare pe fiecare piață | Jurnal și benchmark strict existente; evaluarea specifică noilor module urmează |

Competițiile pot include ligi de cluburi, cupe și naționale. Selecția unei
competiții nu garantează existența cotelor sau a unui istoric suficient.

## Următoarea etapă

1. Construirea tabelelor Daily Tips, Goals Lab și Correct Score peste același
   serviciu de analiză, cu filtre comune de dată și competiție.
2. Extinderea colectării istoricului și verificarea ofertei de cote pentru goluri;
   separarea clară între probabilități calculate și cote efectiv disponibile.
3. Evaluare cronologică separată pentru 1X2, goluri, BTTS și scor exact,
   inclusiv rezultate pe competiții și acoperire. Pragul 85% nu se transferă
   automat de la o piață la alta.
4. Import/export Excel, dacă este util fluxului de lucru, fără a dubla calculele.

## Referințe funcționale

Paginile publice consultate la 21 septembrie 2026 descriu funcții, nu dezvăluie
implementarea algoritmilor proprietari:

- [Combo Goals](https://www.1football-prediction.com/store/combo-goals-football-software):
  indicatori Over 1.5/2.5, medie de goluri și arhivă.
- [Daily Football Tips](https://www.1football-prediction.com/store/daily-football-tips):
  selecții zilnice și mai multe piețe de analiză.
- [Correct Score](https://www.1football-prediction.com/store/correct-score-football-software):
  trei scoruri probabile, 1X2, goluri și BTTS.
