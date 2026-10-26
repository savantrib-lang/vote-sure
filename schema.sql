-- Structure only. No personal data.
PRAGMA foreign_keys=ON;

CREATE TABLE candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        election_id INTEGER NOT NULL,
        name TEXT NOT NULL,
        party_name TEXT NOT NULL,
        symbol_path TEXT,
        photo_path TEXT,
        manifesto TEXT,
        votes_count INTEGER DEFAULT 0,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE
    );

CREATE TABLE elections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'active', -- 'active', 'closed', 'upcoming'
        start_date TEXT,
        end_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

CREATE TABLE users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'officer', -- 'admin' or 'officer'
        full_name TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

CREATE TABLE voters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        voter_id TEXT UNIQUE NOT NULL, -- EPIC Number e.g. TCE-VOT-2026-001
        name TEXT NOT NULL,
        age INTEGER,
        gender TEXT,
        constituency TEXT NOT NULL DEFAULT 'Ward 1 - College Campus',
        contact TEXT,
        email TEXT,
        photo_path TEXT,
        face_encoding BLOB,
        has_voted INTEGER DEFAULT 0, -- 0 = Not Voted, 1 = Already Voted
        voted_at TIMESTAMP,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    );

CREATE TABLE votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        election_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        voter_id INTEGER NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        verification_method TEXT DEFAULT 'Facial Biometric',
        FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
        FOREIGN KEY (voter_id) REFERENCES voters(id) ON DELETE CASCADE,
        UNIQUE(election_id, voter_id) -- Strictest constraint: 1 vote per voter per election
    );

CREATE INDEX idx_candidates_election ON candidates(election_id);

CREATE INDEX idx_votes_candidate ON votes(candidate_id);

CREATE TRIGGER protect_voted_candidate BEFORE DELETE ON candidates
    WHEN EXISTS(SELECT 1 FROM votes WHERE candidate_id=OLD.id)
    BEGIN SELECT RAISE(ABORT, 'Cannot delete a candidate with recorded votes'); END;

CREATE TRIGGER protect_voted_voter BEFORE DELETE ON voters
    WHEN EXISTS(SELECT 1 FROM votes WHERE voter_id=OLD.id)
    BEGIN SELECT RAISE(ABORT, 'Cannot delete a voter with recorded votes'); END;

CREATE TRIGGER validate_ballot BEFORE INSERT ON votes
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM elections WHERE id=NEW.election_id AND status='active'
      ) THEN RAISE(ABORT, 'Election is not active') END;
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM candidates WHERE id=NEW.candidate_id AND election_id=NEW.election_id
      ) THEN RAISE(ABORT, 'Candidate belongs to another election') END;
    END;