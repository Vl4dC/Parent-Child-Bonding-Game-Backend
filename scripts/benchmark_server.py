"""Synthetic local database-path benchmark. No HTTP/auth/model timing is measured.

BENCH_DATABASE_URL=postgresql://localhost/questnest_server_bench uv run python scripts/benchmark_server.py --seed
Only a dedicated local database ending in _bench is accepted. --seed writes synthetic rows.
"""

import argparse
import json
import statistics
import sys
import time
from pathlib import Path

import psycopg
from psycopg.conninfo import conninfo_to_dict
from psycopg.types.json import Jsonb

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from app.platform import progress, resources
from app.repository import PostgresRepository, current_session

OWNER = "00000000-0000-4000-8000-000000000001"


def seed(url):
    with psycopg.connect(url, autocommit=True) as connection:
        connection.execute("create schema if not exists auth")
        connection.execute("""do $$ begin
          if not exists(select 1 from pg_roles where rolname='authenticated') then create role authenticated; end if;
          if not exists(select 1 from pg_roles where rolname='anon') then create role anon; end if;
          if to_regprocedure('auth.uid()') is null then
            execute 'create function auth.uid() returns uuid language sql stable as $f$select (nullif(current_setting(''request.jwt.claims'',true),'''')::jsonb->>''sub'')::uuid$f$';
          end if;
        end $$""")
        connection.execute("grant usage on schema auth to authenticated")
        if connection.execute("select to_regclass('api_private.records')").fetchone()[0] is None:
            for p in sorted(Path("supabase/migrations").glob("*.sql")):
                connection.execute(p.read_text())
        connection.execute("delete from api_private.records where owner_id=%s", (OWNER,))
        rows = []
        for i in range(200):
            rows.append(
                (
                    OWNER,
                    "decks",
                    f"d{i:04}",
                    Jsonb(
                        {
                            "title": "Deck",
                            "lesson_id": "l1",
                            "cards": [{"front": "Q", "back": "x" * 400} for _ in range(20)],
                            "progress": {"reviews": 12},
                        }
                    ),
                )
            )
        for i in range(500):
            rows.append(
                (
                    OWNER,
                    "quizzes",
                    f"q{i:04}",
                    Jsonb(
                        {
                            "attempts": {
                                str(j): {"correct": j % 2 == 0, "choice": 0} for j in range(10)
                            }
                        }
                    ),
                )
            )
        for i in range(100):
            rows.append((OWNER, "completions", f"l{i:04}", Jsonb({"lesson_id": f"l{i:04}"})))
        for i in range(500):
            rows.append(
                (
                    OWNER,
                    "jobs",
                    f"j{i:04}",
                    Jsonb(
                        {
                            "status": "queued" if i < 3 else "succeeded",
                            "input": {"padding": "x" * 2000},
                        }
                    ),
                )
            )
        with connection.cursor() as cursor:
            cursor.executemany(
                "insert into api_private.records(owner_id,kind,id,data) values(%s,%s,%s,%s)", rows
            )
        connection.execute("analyze api_private.records")


def main():
    import os

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--seed", action="store_true")
    args = parser.parse_args()
    url = os.getenv("BENCH_DATABASE_URL", "")
    if not url:
        parser.error("Set BENCH_DATABASE_URL to a dedicated local database ending in _bench")
    info = conninfo_to_dict(url)
    if not info.get("dbname", "").endswith("_bench") or info.get("host", "localhost") not in (
        "localhost",
        "127.0.0.1",
        "::1",
    ):
        parser.error("Only a dedicated local database ending in _bench is accepted")
    if args.seed:
        seed(url)
    repo = PostgresRepository(url)

    def run(fn):
        with repo.session(OWNER, serialize=False) as unit:
            token = current_session.set(unit)
            try:
                return fn(unit)
            finally:
                current_session.reset(token)

    checks = {
        "profile_read": lambda u: u.get("profile", "self"),
        "progress": lambda u: progress(),
        "resource_page": lambda u: resources("decks", after="", limit=50),
        "pending_jobs": lambda u: u.pending_jobs(),
    }
    results = {}
    try:
        for name, fn in checks.items():
            for _ in range(3):
                run(fn)
            samples = []
            for _ in range(20):
                start = time.perf_counter()
                run(fn)
                samples.append((time.perf_counter() - start) * 1000)
            results[name] = {
                "median_ms": round(statistics.median(samples), 3),
                "p95_ms": round(sorted(samples)[18], 3),
            }
        results["data_connections_created"] = repo.pool.get_stats().get("connections_num", 0)
        print(json.dumps(results, indent=2))
    finally:
        repo.close()


if __name__ == "__main__":
    main()
