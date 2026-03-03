#!/usr/bin/env python3
import os, sys, asyncio, glob, pathlib, argparse, time
import asyncpg

LOCK_KEY = 8624001  # advisory lock key

def db_dsn():
    if os.getenv("DATABASE_URL"):
        return os.getenv("DATABASE_URL")
    user = os.getenv("POSTGRES_USER","mevbot")
    pwd  = os.getenv("POSTGRES_PASSWORD","mevbot_pw")
    db   = os.getenv("POSTGRES_DB","mevbot")
    host = os.getenv("POSTGRES_HOST","127.0.0.1")
    port = int(os.getenv("POSTGRES_PORT","5432"))
    return f"postgresql://{user}:{pwd}@{host}:{port}/{db}"

async def ensure_meta(conn):
    await conn.execute("""
    CREATE TABLE IF NOT EXISTS app_schema_migrations (
      id BIGSERIAL PRIMARY KEY,
      filename TEXT UNIQUE NOT NULL,
      applied_at TIMESTAMPTZ NOT NULL DEFAULT now()
    )
    """)

async def applied_set(conn):
    rows = await conn.fetch("SELECT filename FROM app_schema_migrations")
    return {r["filename"] for r in rows}

async def apply_one(conn, fname, dry=False):
    sql = pathlib.Path(fname).read_text()
    print(f"➡️  Applying {fname} ...", flush=True)
    if dry:
        print("   (dry-run)"); return
    async with conn.transaction():
        await conn.execute(sql)
        await conn.execute("INSERT INTO app_schema_migrations(filename) VALUES($1) ON CONFLICT DO NOTHING", os.path.basename(fname))
    print(f"✅ Applied {fname}")

async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="list pending migrations")
    ap.add_argument("--dry-run", action="store_true", help="do not execute, just print")
    ap.add_argument("--one", help="apply a single migration filename")
    ap.add_argument("--baseline", help="mark a migration as applied without running it")
    args = ap.parse_args()

    dsn = db_dsn()
    conn = await asyncpg.connect(dsn)
    try:
        # advisory lock to serialize
        await conn.execute("SELECT pg_advisory_lock($1)", LOCK_KEY)
        await ensure_meta(conn)

        raw_files = sorted(glob.glob(os.getenv("MIGRATIONS_DIR","migrations") + "/*.sql"))
        files = [f for f in raw_files if pathlib.Path(f).is_file()]
        skipped = [f for f in raw_files if not pathlib.Path(f).is_file()]
        for f in skipped:
            print(f"⚠️  Skipping missing migration target: {f}")
        have = await applied_set(conn)

        if args.baseline:
            base = os.path.basename(args.baseline)
            if base not in [os.path.basename(f) for f in files]:
                print(f"❌ baseline file not found in migrations/: {base}")
                sys.exit(1)
            await conn.execute("INSERT INTO app_schema_migrations(filename) VALUES($1) ON CONFLICT DO NOTHING", base)
            print(f"✅ Baseline recorded: {base}")
            return

        if args.list:
            pending = [f for f in files if os.path.basename(f) not in have]
            print("Pending migrations:")
            for f in pending: print(" -", os.path.basename(f))
            return

        targets = []
        if args.one:
            f = [f for f in files if os.path.basename(f) == os.path.basename(args.one)]
            if not f:
                print(f"❌ not found: {args.one}"); sys.exit(1)
            if os.path.basename(f[0]) in have:
                print(f"ℹ️  already applied: {args.one}"); return
            targets = f
        else:
            targets = [f for f in files if os.path.basename(f) not in have]

        if not targets:
            print("👍 No pending migrations."); return

        for f in targets:
            await apply_one(conn, f, dry=args.dry_run)
    finally:
        await conn.execute("SELECT pg_advisory_unlock($1)", LOCK_KEY)
        await conn.close()

if __name__ == "__main__":
    asyncio.run(main())
