"""Download public match archives; keep raw files, provenance, and SHA256 hashes."""

import asyncio
import csv
import hashlib
import io
import json
from datetime import datetime, timezone
from pathlib import Path

import httpx

from app.config import ROOT
from app.domain import Match

DATA_DIR = ROOT / "data/benchmark"
LEAGUES = {
    "E0": "Premier League",
    "SP1": "La Liga",
    "D1": "Bundesliga",
    "I1": "Serie A",
    "F1": "Ligue 1",
}


def parse_archive(content, league, season):
    reader = csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="strict")))
    required = {"Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"}
    if not required.issubset(reader.fieldnames or []):
        raise ValueError(f"Schema CSV invalidă: {league}/{season}")
    records, seen = [], set()
    for rownum, row in enumerate(reader, 2):
        if not row.get("Date"):
            continue
        if not row.get("FTHG") or not row.get("FTAG"):
            raise ValueError(f"Rezultat incomplet: {league}/{season}:{rownum}")
        raw_date = row["Date"].strip()
        fmt = "%d/%m/%Y" if len(raw_date.split("/")[-1]) == 4 else "%d/%m/%y"
        kickoff = datetime.strptime(raw_date, fmt).replace(tzinfo=timezone.utc)
        if kickoff >= datetime.now(timezone.utc):
            raise ValueError("Arhiva conține un rezultat cu dată în viitor.")
        match_id = f"fd-{league}-{kickoff.date()}-{row['HomeTeam']}-{row['AwayTeam']}"
        if match_id in seen:
            raise ValueError(f"Duplicat: {match_id}")
        seen.add(match_id)
        match = Match(
            id=match_id,
            kickoff=kickoff,
            league=LEAGUES[league],
            home=row["HomeTeam"],
            away=row["AwayTeam"],
            status="finished",
            home_goals=int(row["FTHG"]),
            away_goals=int(row["FTAG"]),
            source="football-data.co.uk",
        )
        reference_odds = {}
        for key, column in (("1", "AvgH"), ("X", "AvgD"), ("2", "AvgA")):
            try:
                value = float(row.get(column) or 0)
                if 1 < value < 1001:
                    reference_odds[key] = value
            except ValueError:
                pass
        records.append(
            {
                "match": match.model_dump(mode="json"),
                "season": season,
                "league_code": league,
                "reference_odds": reference_odds,
            }
        )
    if len(records) < 250:
        raise ValueError(f"Saison incomplet suspect: {league}/{season}, {len(records)} meciuri")
    return records


async def download(protocol, directory=DATA_DIR):
    directory.mkdir(parents=True, exist_ok=True)
    raw = directory / "raw"
    raw.mkdir(exist_ok=True)
    records, manifest = [], []
    async with httpx.AsyncClient(timeout=40, follow_redirects=True) as client:
        # Deliberately sequential, with disk cache; 20 small CSV requests on the first run.
        for season in protocol["history_seasons"] + [protocol["test_season"]]:
            for league in protocol["leagues"]:
                url = f"https://www.football-data.co.uk/mmz4281/{season}/{league}.csv"
                path = raw / f"{season}-{league}.csv"
                if not path.exists():
                    response = await client.get(url)
                    response.raise_for_status()
                    # Validate before caching a response (e.g. HTML error with HTTP 200).
                    parse_archive(response.content, league, season)
                    path.write_bytes(response.content)
                    await asyncio.sleep(0.15)
                content = path.read_bytes()
                parsed = parse_archive(content, league, season)
                records.extend(parsed)
                manifest.append(
                    {
                        "url": url,
                        "file": path.name,
                        "matches": len(parsed),
                        "sha256": hashlib.sha256(content).hexdigest(),
                    }
                )
                print(f"{season}/{league}: {len(parsed)} matches", flush=True)
    ids = [r["match"]["id"] for r in records]
    if len(ids) != len(set(ids)):
        raise ValueError("Duplicate match IDs across archives.")
    destination = directory / "matches.jsonl"
    destination.write_text("\n".join(json.dumps(r) for r in records) + "\n", encoding="utf-8")
    metadata = {
        "source": "https://www.football-data.co.uk/data.php",
        "notes": "https://www.football-data.co.uk/notes.txt",
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "files": manifest,
        "matches": len(records),
        "dataset_sha256": hashlib.sha256(destination.read_bytes()).hexdigest(),
    }
    (directory / "manifest.json").write_text(json.dumps(metadata, indent=2), encoding="utf-8")
    return metadata


def load(directory=DATA_DIR):
    raw = (directory / "matches.jsonl").read_bytes()
    metadata = json.loads((directory / "manifest.json").read_text(encoding="utf-8"))
    if hashlib.sha256(raw).hexdigest() != metadata["dataset_sha256"]:
        raise ValueError("Dataset checksum mismatch. Recreate the benchmark dataset.")
    return [json.loads(line) for line in raw.decode("utf-8").splitlines() if line], metadata


if __name__ == "__main__":
    protocol = json.loads((Path(__file__).parent / "protocol.json").read_text(encoding="utf-8"))
    print(json.dumps(asyncio.run(download(protocol)), indent=2))
