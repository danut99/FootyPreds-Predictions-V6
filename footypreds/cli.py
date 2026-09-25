"""Bounded historical ingestion: python -m footypreds.cli collect --start ... --end ..."""

import argparse
import asyncio
from datetime import date, timedelta

from footypreds.config import Settings
from footypreds.engine import HistoryIndex, analyze, backtest
from footypreds.excel import build_workbook
from footypreds.provider import FlashScore, ProviderError
from footypreds.store import Store


async def collect(start, end, provider):
    if start > end or (end - start).days > 30 or end > date.today():
        raise ValueError("Alege maximum 31 zile, în ordine, fără date viitoare.")
    day = start
    try:
        while day <= end:
            matches, cached, rejected = await provider.fixtures(day)
            provider.store.save_matches(matches)
            provider.store.settle(matches)
            print(f"{day}: {len(matches)} meciuri, cache={cached}, ignorate={rejected}")
            day += timedelta(days=1)
    finally:
        await provider.client.aclose()


async def export(day, output, provider):
    try:
        matches, _, _ = await provider.fixtures(day)
    finally:
        await provider.client.aclose()
    provider.store.save_matches(matches)
    index = HistoryIndex(provider.store.matches(sport="football"))
    items = [(m, analyze(m, index)) for m in matches if m.status != "unavailable"]
    path = output or f"FootyPreds-{day.isoformat()}.xlsx"
    with open(path, "wb") as handle:
        handle.write(build_workbook(day, items))
    print(f"{len(items)} meciuri exportate în {path}")


def main():
    parser = argparse.ArgumentParser(description="FootyPreds V7 data tools")
    commands = parser.add_subparsers(dest="command", required=True)
    collect_parser = commands.add_parser("collect")
    collect_parser.add_argument("--start", type=date.fromisoformat, required=True)
    collect_parser.add_argument("--end", type=date.fromisoformat, required=True)
    export_parser = commands.add_parser("export", help="Excel cu predicțiile unei zile")
    export_parser.add_argument("--day", type=date.fromisoformat, required=True)
    export_parser.add_argument("--output", default="")
    backtest_parser = commands.add_parser("backtest")
    backtest_parser.add_argument("--threshold", type=float, default=0.85)
    args = parser.parse_args()
    settings = Settings.load()
    store = Store(settings.database)
    try:
        if args.command == "collect":
            asyncio.run(collect(args.start, args.end, FlashScore(settings, store)))
        elif args.command == "export":
            asyncio.run(export(args.day, args.output, FlashScore(settings, store)))
        else:
            if not 0.5 <= args.threshold <= 0.99:
                raise ValueError("Pragul trebuie să fie între 0.5 și 0.99.")
            import json

            matches = [m for m in store.matches(sport="football") if m.status == "finished"][-3000:]
            print(json.dumps(backtest(matches, args.threshold)["metrics"], indent=2))
    except (ProviderError, ValueError) as exc:
        parser.exit(1, str(exc) + "\n")


if __name__ == "__main__":
    main()
