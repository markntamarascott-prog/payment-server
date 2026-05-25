CREATE TABLE payments (
    id SERIAL PRIMARY KEY,
    session_id TEXT UNIQUE,
    status TEXT,
    created_at TIMESTAMP
);