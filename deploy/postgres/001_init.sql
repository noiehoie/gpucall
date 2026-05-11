CREATE TABLE IF NOT EXISTS gpucall_jobs (
  job_id TEXT PRIMARY KEY,
  owner_identity TEXT,
  state TEXT NOT NULL,
  payload JSONB NOT NULL,
  created_at TIMESTAMPTZ NOT NULL DEFAULT now(),
  updated_at TIMESTAMPTZ NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS gpucall_idempotency (
  key TEXT PRIMARY KEY,
  created_at DOUBLE PRECISION NOT NULL,
  request_hash TEXT NOT NULL,
  status INTEGER NOT NULL,
  content JSONB NOT NULL,
  headers JSONB NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS gpucall_jobs_owner_state_idx ON gpucall_jobs(owner_identity, state);
CREATE INDEX IF NOT EXISTS gpucall_idempotency_created_at_idx ON gpucall_idempotency(created_at);
