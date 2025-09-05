import os, glob, psycopg
from pathlib import Path

def conn_str():
    host = os.getenv("POSTGRES_HOST","mev-db")
    db = os.getenv("POSTGRES_DB","mev_bot")
    user = os.getenv("POSTGRES_USER","mev_user")
    pwd = os.getenv("POSTGRES_PASSWORD","change_me")
    ssl = os.getenv("POSTGRES_SSLMODE","disable")
    return f"host={host} dbname={db} user={user} password={pwd} sslmode={ssl}"

def applied(cur):
    cur.execute("CREATE TABLE IF NOT EXISTS schema_migrations(id serial primary key, version text unique not null, applied_at timestamptz default now());")
    cur.execute("SELECT version FROM schema_migrations")
    return {r[0] for r in cur.fetchall()}

def main():
    files = sorted(glob.glob("sql/migrations/*.sql"))
    with psycopg.connect(conn_str()) as con:
        with con.cursor() as cur:
            done = applied(cur)
            for fp in files:
                ver = Path(fp).stem
                if ver in done: continue
                sql = Path(fp).read_text()
                cur.execute(sql)
                con.commit()
    print("migrations ok")

if __name__ == "__main__":
    main()
