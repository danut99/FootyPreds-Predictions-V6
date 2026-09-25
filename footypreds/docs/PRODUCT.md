# Produsul FootyPreds V8

Referință funcțională: [1football-prediction.com](https://1football-prediction.com/)
(analizat pe 25 septembrie 2026). Site-ul vinde unelte Windows/Excel pe piețe
(Correct Score, Just Goals, Daily Tips, HT/FT, Combo Goals) și publică zilnic predicții
gratuite dintr-un model Poisson atac/apărare (timp de înjumătățire 180 zile, shrinkage),
cu un track record public și benzi de calibrare („modelul a spus X% → s-a câștigat Y%”).
Nu am copiat conținut sau cod; am preluat structura funcțională.

## Două clienți peste același API

| Client | Pentru cine | Unde |
|---|---|---|
| Aplicația web | analiză zilnică în browser | `footypreds/web/`, http://127.0.0.1:8000 |
| Clientul Excel | lucru în foi de calcul, ca uneltele Excel 1football | `footypreds/excel_client/` |

Ambele citesc același motor, prin HTTP; Excel nu recalculează nimic.

## Module

| Modul | Ce oferă |
|---|---|
| Predicții zilnice | toate meciurile zilei, grupate pe competiții; tab-uri 1X2, Goluri, GG, Scor corect, Pauză/Final; notă A–D; ponturile zilei |
| Pagina de meci | rezumat, 1X2 model vs. piață, xG, scor corect (hartă 6×6 + top 5), total goluri, pauză/final, toate piețele cu cotă corectă și EV, formă ultimele 5/10 + acasă/deplasare + serii, H2H, clasament, observații automate |
| Analiză completă | o cerere FlashScore `matches/h2h` aduce ~50 de meciuri recente/echipă din toate competițiile; plus clasamentul |
| Sincronizare istoric | rezultatele zilelor trecute, o cerere pe zi, niciodată repetată pentru zilele încheiate |
| Bilete | plan 7 zile sau bilet pe o zi, cote 1X2 reale, ±10% față de ținta aleasă |
| Track record | prima selecție pre-meci, decontată automat; rată de reușită, Wilson 95%, benzi de calibrare |
| Export Excel | `/api/export.xlsx`: foi Predicții, Scor corect, Formă, Valoare, Pauză-Final, Legendă |

## Consum RapidAPI

- tabla zilei: 1 cerere/zi (cache 15 minute; zilele trecute 30 zile);
- analiză completă: 2 cereri/meci (H2H + clasament; cache 6 ore);
- sincronizare istoric: 1 cerere/zi nesincronizată;
- bilete: maximum 8 analize complete pe zi, doar când datele locale nu ajung.
