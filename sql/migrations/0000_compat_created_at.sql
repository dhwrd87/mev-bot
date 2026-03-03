DO $$
DECLARE r record;
BEGIN
  -- Add created_at where missing on all public tables (dev-safe)
  FOR r IN
    SELECT table_schema, table_name
    FROM information_schema.tables
    WHERE table_schema = 'public' AND table_type = 'BASE TABLE'
  LOOP
    EXECUTE format('ALTER TABLE %I.%I ADD COLUMN IF NOT EXISTS created_at timestamptz', r.table_schema, r.table_name);
    EXECUTE format('ALTER TABLE %I.%I ALTER COLUMN created_at SET DEFAULT now()', r.table_schema, r.table_name);
  END LOOP;
END$$;
