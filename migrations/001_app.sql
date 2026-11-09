CREATE TABLE IF NOT EXISTS policies (
 id TEXT PRIMARY KEY, parent TEXT, config TEXT NOT NULL, status TEXT NOT NULL,
 created REAL NOT NULL, evidence TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS active (singleton INTEGER PRIMARY KEY CHECK(singleton=1), policy TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS policy_audit (id INTEGER PRIMARY KEY, policy TEXT, action TEXT, evidence TEXT, at REAL);
CREATE TABLE IF NOT EXISTS observations (
 policy TEXT, session TEXT, success INTEGER, critical INTEGER, PRIMARY KEY(policy,session)
);
CREATE TABLE IF NOT EXISTS sessions (
 id TEXT PRIMARY KEY, token_hash TEXT NOT NULL, expires REAL NOT NULL, scope TEXT,
 verified_until REAL, appointment TEXT NOT NULL, policy TEXT NOT NULL, state TEXT NOT NULL,
 revision INTEGER NOT NULL DEFAULT 0, turn INTEGER NOT NULL DEFAULT 0,
 confirmation TEXT, reply TEXT NOT NULL, fault TEXT, closed INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, id TEXT UNIQUE NOT NULL, session TEXT NOT NULL,
 turn INTEGER NOT NULL, type TEXT NOT NULL, payload TEXT NOT NULL, at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS desk_calls (
 session TEXT PRIMARY KEY, desk TEXT NOT NULL, expected_scope TEXT NOT NULL, created REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS desk_call_order ON desk_calls(desk, created, session);
CREATE INDEX IF NOT EXISTS session_events ON events(session,sequence);
CREATE TABLE IF NOT EXISTS confirmations (
 id TEXT PRIMARY KEY, session TEXT NOT NULL, scope TEXT NOT NULL, revision INTEGER NOT NULL,
 params TEXT NOT NULL, digest TEXT NOT NULL, expires REAL NOT NULL, status TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS operations (
 id TEXT PRIMARY KEY, session TEXT NOT NULL, confirmation TEXT NOT NULL, request TEXT NOT NULL,
 state TEXT NOT NULL, receipt TEXT, fault TEXT, attempts INTEGER NOT NULL DEFAULT 0,
 next_at REAL NOT NULL DEFAULT 0
);
CREATE UNIQUE INDEX IF NOT EXISTS one_pending ON operations(session)
 WHERE state IN ('PREPARED','SENT','UNKNOWN');
CREATE TABLE IF NOT EXISTS results (operation TEXT PRIMARY KEY, session TEXT NOT NULL, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS session_results (session TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS reports (session TEXT PRIMARY KEY, data TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS jobs (
 id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
 created REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS validation_queries (
 batch TEXT NOT NULL, candidate TEXT NOT NULL UNIQUE, created REAL NOT NULL
);
