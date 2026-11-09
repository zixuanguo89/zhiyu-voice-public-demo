CREATE TABLE IF NOT EXISTS appointments (
 space TEXT, id TEXT, owner TEXT NOT NULL, slot TEXT NOT NULL, version INTEGER NOT NULL,
 status TEXT NOT NULL, PRIMARY KEY(space,id)
);
CREATE TABLE IF NOT EXISTS slots (
 space TEXT, id TEXT, start TEXT NOT NULL, capacity INTEGER NOT NULL, PRIMARY KEY(space,id)
);
CREATE TABLE IF NOT EXISTS bank_rolling_schedule (
 space TEXT PRIMARY KEY
);
CREATE TABLE IF NOT EXISTS receipts (
 space TEXT, operation TEXT, digest TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(space,operation)
);
CREATE TABLE IF NOT EXISTS handoffs (
 space TEXT, session TEXT, data TEXT NOT NULL, PRIMARY KEY(space,session)
);
CREATE TABLE IF NOT EXISTS bank_customers (
 space TEXT NOT NULL, number INTEGER NOT NULL, name TEXT NOT NULL,
 phone_key TEXT NOT NULL, phone_tail TEXT NOT NULL,
 PRIMARY KEY(space,number), UNIQUE(space,name,phone_key)
);
