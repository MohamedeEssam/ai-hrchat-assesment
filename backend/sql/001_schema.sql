CREATE EXTENSION IF NOT EXISTS vector;

DO $$
BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'migrator') THEN
    CREATE ROLE migrator LOGIN PASSWORD 'migrator_pass';
  END IF;
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'app_user') THEN
    CREATE ROLE app_user LOGIN PASSWORD 'app_pass';
  END IF;
END$$;

CREATE TABLE IF NOT EXISTS companies (
  id text PRIMARY KEY,
  name text NOT NULL
);

CREATE TABLE IF NOT EXISTS users (
  id text NOT NULL,
  company_id text NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  role text NOT NULL CHECK (role IN ('employee', 'hr')),
  token text NOT NULL UNIQUE,
  PRIMARY KEY (id, company_id)
);

CREATE TABLE IF NOT EXISTS documents (
  id uuid PRIMARY KEY,
  company_id text NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  title text NOT NULL,
  content text NOT NULL,
  visibility text NOT NULL CHECK (visibility IN ('everyone', 'restricted')),
  allowed_roles text[] NOT NULL DEFAULT '{}',
  UNIQUE (id, company_id)
);

CREATE TABLE IF NOT EXISTS chunks (
  id uuid PRIMARY KEY,
  document_id uuid NOT NULL,
  company_id text NOT NULL,
  text text NOT NULL,
  embedding vector(384) NOT NULL,
  FOREIGN KEY (document_id, company_id) REFERENCES documents(id, company_id) ON DELETE CASCADE
);

ALTER TABLE chunks ADD COLUMN IF NOT EXISTS visibility text;
ALTER TABLE chunks ADD COLUMN IF NOT EXISTS allowed_roles text[];
UPDATE chunks c
SET visibility = d.visibility,
    allowed_roles = d.allowed_roles
FROM documents d
WHERE c.document_id = d.id
  AND c.company_id = d.company_id
  AND (c.visibility IS NULL OR c.allowed_roles IS NULL);
ALTER TABLE chunks ALTER COLUMN visibility SET DEFAULT 'everyone';
ALTER TABLE chunks ALTER COLUMN allowed_roles SET DEFAULT '{}';
ALTER TABLE chunks ALTER COLUMN visibility SET NOT NULL;
ALTER TABLE chunks ALTER COLUMN allowed_roles SET NOT NULL;
UPDATE chunks
SET visibility = 'everyone'
WHERE visibility IS NULL;
UPDATE chunks
SET allowed_roles = '{}'
WHERE allowed_roles IS NULL;

CREATE INDEX IF NOT EXISTS chunks_hnsw_idx ON chunks USING hnsw (embedding vector_cosine_ops);

CREATE TABLE IF NOT EXISTS leave_balances (
  company_id text NOT NULL,
  user_id text NOT NULL,
  days integer NOT NULL CHECK (days >= 0),
  PRIMARY KEY (company_id, user_id),
  FOREIGN KEY (user_id, company_id) REFERENCES users(id, company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS runs (
  id uuid PRIMARY KEY,
  company_id text NOT NULL REFERENCES companies(id) ON DELETE CASCADE,
  user_id text NOT NULL,
  goal text NOT NULL,
  status text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  FOREIGN KEY (user_id, company_id) REFERENCES users(id, company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS run_inputs (
  run_id uuid NOT NULL,
  company_id text NOT NULL,
  seq integer NOT NULL,
  text text NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, seq),
  FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS run_events (
  run_id uuid NOT NULL,
  company_id text NOT NULL,
  seq integer NOT NULL,
  type text NOT NULL,
  payload jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (run_id, seq),
  FOREIGN KEY (run_id) REFERENCES runs(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS proposals (
  id uuid PRIMARY KEY,
  run_id uuid NOT NULL REFERENCES runs(id) ON DELETE CASCADE,
  company_id text NOT NULL,
  owner_user_id text NOT NULL,
  kind text NOT NULL,
  payload jsonb NOT NULL,
  status text NOT NULL,
  supersedes_id uuid NULL,
  expires_at timestamptz NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now(),
  confirmed_at timestamptz NULL,
  FOREIGN KEY (owner_user_id, company_id) REFERENCES users(id, company_id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS audit_log (
  id bigserial PRIMARY KEY,
  company_id text NOT NULL,
  user_id text NOT NULL,
  action text NOT NULL,
  details jsonb NOT NULL,
  created_at timestamptz NOT NULL DEFAULT now()
);

DROP POLICY IF EXISTS company_scope_companies ON companies;
DROP POLICY IF EXISTS company_scope_users ON users;
DROP POLICY IF EXISTS company_scope_documents ON documents;
DROP POLICY IF EXISTS company_scope_chunks ON chunks;
DROP POLICY IF EXISTS company_scope_leave ON leave_balances;
DROP POLICY IF EXISTS company_scope_runs ON runs;
DROP POLICY IF EXISTS company_scope_run_inputs ON run_inputs;
DROP POLICY IF EXISTS company_scope_run_events ON run_events;
DROP POLICY IF EXISTS company_scope_proposals ON proposals;
DROP POLICY IF EXISTS company_scope_audit ON audit_log;

ALTER TABLE companies ENABLE ROW LEVEL SECURITY;
ALTER TABLE users ENABLE ROW LEVEL SECURITY;
ALTER TABLE documents ENABLE ROW LEVEL SECURITY;
ALTER TABLE chunks ENABLE ROW LEVEL SECURITY;
ALTER TABLE leave_balances ENABLE ROW LEVEL SECURITY;
ALTER TABLE runs ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_inputs ENABLE ROW LEVEL SECURITY;
ALTER TABLE run_events ENABLE ROW LEVEL SECURITY;
ALTER TABLE proposals ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log ENABLE ROW LEVEL SECURITY;

ALTER TABLE companies FORCE ROW LEVEL SECURITY;
ALTER TABLE users FORCE ROW LEVEL SECURITY;
ALTER TABLE documents FORCE ROW LEVEL SECURITY;
ALTER TABLE chunks FORCE ROW LEVEL SECURITY;
ALTER TABLE leave_balances FORCE ROW LEVEL SECURITY;
ALTER TABLE runs FORCE ROW LEVEL SECURITY;
ALTER TABLE run_inputs FORCE ROW LEVEL SECURITY;
ALTER TABLE run_events FORCE ROW LEVEL SECURITY;
ALTER TABLE proposals FORCE ROW LEVEL SECURITY;
ALTER TABLE audit_log FORCE ROW LEVEL SECURITY;

CREATE POLICY company_scope_companies ON companies
  USING (id = current_setting('app.company_id', true))
  WITH CHECK (id = current_setting('app.company_id', true));

CREATE POLICY company_scope_users ON users
  USING (company_id = current_setting('app.company_id', true))
  WITH CHECK (company_id = current_setting('app.company_id', true));

CREATE POLICY company_scope_documents ON documents
  USING (
    company_id = current_setting('app.company_id', true)
    AND (
      visibility = 'everyone'
      OR current_setting('app.role', true) = ANY(allowed_roles)
    )
  )
  WITH CHECK (company_id = current_setting('app.company_id', true));

CREATE POLICY company_scope_chunks ON chunks
  USING (
    company_id = current_setting('app.company_id', true)
    AND (
      visibility = 'everyone'
      OR current_setting('app.role', true) = ANY(allowed_roles)
    )
  )
  WITH CHECK (company_id = current_setting('app.company_id', true));

CREATE POLICY company_scope_leave ON leave_balances
  USING (
    company_id = current_setting('app.company_id', true)
    AND (
      user_id = current_setting('app.user_id', true)
      OR current_setting('app.role', true) = 'hr'
    )
  )
  WITH CHECK (
    company_id = current_setting('app.company_id', true)
    AND (
      user_id = current_setting('app.user_id', true)
      OR current_setting('app.role', true) = 'hr'
    )
  );

CREATE POLICY company_scope_runs ON runs
  USING (
    company_id = current_setting('app.company_id', true)
    AND (
      user_id = current_setting('app.user_id', true)
      OR current_setting('app.role', true) = 'hr'
    )
  )
  WITH CHECK (
    company_id = current_setting('app.company_id', true)
    AND (
      user_id = current_setting('app.user_id', true)
      OR current_setting('app.role', true) = 'hr'
    )
  );

CREATE POLICY company_scope_run_inputs ON run_inputs
  USING (
    company_id = current_setting('app.company_id', true)
    AND run_id IN (
      SELECT id FROM runs
      WHERE company_id = current_setting('app.company_id', true)
        AND (
          user_id = current_setting('app.user_id', true)
          OR current_setting('app.role', true) = 'hr'
        )
    )
  )
  WITH CHECK (
    company_id = current_setting('app.company_id', true)
    AND run_id IN (
      SELECT id FROM runs
      WHERE company_id = current_setting('app.company_id', true)
        AND (
          user_id = current_setting('app.user_id', true)
          OR current_setting('app.role', true) = 'hr'
        )
    )
  );

CREATE POLICY company_scope_run_events ON run_events
  USING (
    company_id = current_setting('app.company_id', true)
    AND run_id IN (
      SELECT id FROM runs
      WHERE company_id = current_setting('app.company_id', true)
        AND (
          user_id = current_setting('app.user_id', true)
          OR current_setting('app.role', true) = 'hr'
        )
    )
  )
  WITH CHECK (
    company_id = current_setting('app.company_id', true)
    AND run_id IN (
      SELECT id FROM runs
      WHERE company_id = current_setting('app.company_id', true)
        AND (
          user_id = current_setting('app.user_id', true)
          OR current_setting('app.role', true) = 'hr'
        )
    )
  );

CREATE POLICY company_scope_proposals ON proposals
  USING (
    company_id = current_setting('app.company_id', true)
    AND (
      owner_user_id = current_setting('app.user_id', true)
      OR current_setting('app.role', true) = 'hr'
    )
  )
  WITH CHECK (
    company_id = current_setting('app.company_id', true)
    AND (
      owner_user_id = current_setting('app.user_id', true)
      OR current_setting('app.role', true) = 'hr'
    )
  );

CREATE POLICY company_scope_audit ON audit_log
  USING (company_id = current_setting('app.company_id', true))
  WITH CHECK (company_id = current_setting('app.company_id', true));

GRANT USAGE ON SCHEMA public TO app_user, migrator;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA public TO app_user, migrator;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA public TO app_user, migrator;

ALTER DEFAULT PRIVILEGES IN SCHEMA public
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO app_user, migrator;

ALTER ROLE app_user NOBYPASSRLS;
