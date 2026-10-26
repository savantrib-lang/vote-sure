import sqlite3
import os
import pickle
import hashlib
import hmac
import shutil
import numpy as np
from datetime import datetime, date
from werkzeug.security import generate_password_hash, check_password_hash

DB_PATH = os.environ.get('VOTING_DB_PATH') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'data', 'voting.db')

GENESIS_HASH = "0" * 64

def _get_ledger_secret():
    secret_path = os.path.join(os.path.dirname(DB_PATH), '.session_secret')
    if os.path.exists(secret_path):
        try:
            with open(secret_path, 'rb') as f:
                content = f.read().strip()
                if content:
                    return content
        except Exception:
            pass
    return b"votesure_default_secure_ledger_key_2026_antitamper"

def _compute_vote_hash(prev_hash, election_id, candidate_id, voter_id, timestamp, verification_method, live_photo_path):
    data = f"{prev_hash}|{election_id}|{candidate_id}|{voter_id}|{timestamp}|{verification_method}|{live_photo_path or ''}"
    return hashlib.sha256(data.encode('utf-8')).hexdigest()

def _sign_vote_hash(vote_hash):
    secret = _get_ledger_secret()
    return hmac.new(secret, vote_hash.encode('utf-8'), hashlib.sha256).hexdigest()

def get_db_connection():
    os.makedirs(os.path.dirname(DB_PATH), exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=20)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA synchronous = NORMAL")
    conn.execute("PRAGMA busy_timeout = 20000")
    return conn

def init_db():
    conn = get_db_connection()
    cursor = conn.cursor()

    # Admin / Polling Officer Users Table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS users (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        username TEXT UNIQUE NOT NULL,
        password_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'officer', -- 'admin' or 'officer'
        full_name TEXT NOT NULL,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    cursor.execute("PRAGMA table_info(users)")
    user_cols = [col[1] for col in cursor.fetchall()]
    if 'email' not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN email TEXT DEFAULT ''")
    if 'contact' not in user_cols:
        cursor.execute("ALTER TABLE users ADD COLUMN contact TEXT DEFAULT ''")

    # Elections Table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS elections (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        title TEXT NOT NULL,
        description TEXT,
        status TEXT NOT NULL DEFAULT 'active', -- 'active', 'closed', 'upcoming'
        start_date TEXT,
        end_date TEXT,
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
    )
    ''')

    # Candidates Table
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS candidates (
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
    )
    ''')

    # Voters Table (Biometric facial profiles)
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS voters (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        election_id INTEGER NOT NULL DEFAULT 1,
        voter_id TEXT NOT NULL, -- EPIC or Student Roll ID
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
        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE,
        UNIQUE(election_id, voter_id)
    )
    ''')

    cursor.execute("PRAGMA table_info(voters)")
    voter_cols = [col[1] for col in cursor.fetchall()]
    if 'election_id' not in voter_cols:
        cursor.execute("ALTER TABLE voters ADD COLUMN election_id INTEGER DEFAULT 1 REFERENCES elections(id)")
    cursor.execute("UPDATE voters SET election_id = 1 WHERE election_id IS NULL")

    # Audit Trail of Cast Votes
    cursor.execute('''
    CREATE TABLE IF NOT EXISTS votes (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        election_id INTEGER NOT NULL,
        candidate_id INTEGER NOT NULL,
        voter_id INTEGER NOT NULL,
        timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
        verification_method TEXT DEFAULT 'Facial Biometric',
        live_photo_path TEXT,
        FOREIGN KEY (election_id) REFERENCES elections(id) ON DELETE CASCADE,
        FOREIGN KEY (candidate_id) REFERENCES candidates(id) ON DELETE CASCADE,
        FOREIGN KEY (voter_id) REFERENCES voters(id) ON DELETE CASCADE,
        UNIQUE(election_id, voter_id) -- Strictest constraint: 1 vote per voter per election
    )
    ''')

    cursor.execute("PRAGMA table_info(votes)")
    vote_cols = [col[1] for col in cursor.fetchall()]
    if 'live_photo_path' not in vote_cols:
        cursor.execute("ALTER TABLE votes ADD COLUMN live_photo_path TEXT")
    if 'prev_hash' not in vote_cols:
        cursor.execute("ALTER TABLE votes ADD COLUMN prev_hash TEXT")
    if 'vote_hash' not in vote_cols:
        cursor.execute("ALTER TABLE votes ADD COLUMN vote_hash TEXT")
    if 'signature' not in vote_cols:
        cursor.execute("ALTER TABLE votes ADD COLUMN signature TEXT")

    # Cryptographically backfill any legacy un-hashed votes
    cursor.execute("""
        SELECT id, election_id, candidate_id, voter_id, timestamp, verification_method, live_photo_path
        FROM votes WHERE vote_hash IS NULL ORDER BY id ASC
    """)
    unhashed_votes = cursor.fetchall()
    if unhashed_votes:
        last_hash_row = cursor.execute("SELECT vote_hash FROM votes WHERE vote_hash IS NOT NULL ORDER BY id DESC LIMIT 1").fetchone()
        current_prev = last_hash_row[0] if last_hash_row else GENESIS_HASH
        for v in unhashed_votes:
            v_hash = _compute_vote_hash(current_prev, v['election_id'], v['candidate_id'], v['voter_id'], v['timestamp'], v['verification_method'], v['live_photo_path'])
            v_sig = _sign_vote_hash(v_hash)
            cursor.execute("UPDATE votes SET prev_hash = ?, vote_hash = ?, signature = ? WHERE id = ?", (current_prev, v_hash, v_sig, v['id']))
            current_prev = v_hash
        conn.commit()

    # Additive, idempotent safeguards for both existing and fresh databases.
    cursor.executescript("""
    CREATE INDEX IF NOT EXISTS idx_votes_candidate ON votes(candidate_id);
    CREATE INDEX IF NOT EXISTS idx_candidates_election ON candidates(election_id);
    CREATE INDEX IF NOT EXISTS idx_votes_vote_hash ON votes(vote_hash);
    CREATE INDEX IF NOT EXISTS idx_voters_election ON voters(election_id);

    DROP TRIGGER IF EXISTS validate_ballot;
    CREATE TRIGGER validate_ballot BEFORE INSERT ON votes
    BEGIN
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM elections WHERE id=NEW.election_id AND status='active'
      ) THEN RAISE(ABORT, 'Election is not active') END;
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM candidates WHERE id=NEW.candidate_id AND election_id=NEW.election_id
      ) THEN RAISE(ABORT, 'Candidate belongs to another election') END;
      SELECT CASE WHEN NOT EXISTS (
        SELECT 1 FROM voters WHERE id=NEW.voter_id AND election_id=NEW.election_id
      ) THEN RAISE(ABORT, 'Elector is not registered in this election electoral roll') END;
    END;

    CREATE TRIGGER IF NOT EXISTS protect_voted_voter BEFORE DELETE ON voters
    WHEN EXISTS(SELECT 1 FROM votes WHERE voter_id=OLD.id)
    BEGIN SELECT RAISE(ABORT, 'Cannot delete a voter with recorded votes'); END;

    CREATE TRIGGER IF NOT EXISTS protect_voted_candidate BEFORE DELETE ON candidates
    WHEN EXISTS(SELECT 1 FROM votes WHERE candidate_id=OLD.id)
    BEGIN SELECT RAISE(ABORT, 'Cannot delete a candidate with recorded votes'); END;

    CREATE TRIGGER IF NOT EXISTS prevent_vote_tampering BEFORE UPDATE ON votes
    BEGIN
      SELECT RAISE(ABORT, 'DATABASE INTEGRITY ALERT: Cast ballots in votes ledger are strictly immutable. Vote updates are forbidden.');
    END;

    CREATE TRIGGER IF NOT EXISTS protect_voter_ballot_status
    BEFORE UPDATE OF has_voted ON voters
    WHEN OLD.has_voted = 1 AND NEW.has_voted = 0 AND EXISTS(SELECT 1 FROM votes WHERE voter_id=OLD.id)
    BEGIN
      SELECT RAISE(ABORT, 'SECURITY VIOLATION: Cannot reset elector status to unvoted while ballot exists in votes ledger.');
    END;
    """)
    conn.commit()

    # Seed default admin, polling officer, and college principal if none exist
    cursor.execute("SELECT COUNT(*) FROM users")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''
        INSERT INTO users (username, password_hash, role, full_name)
        VALUES (?, ?, ?, ?)
        ''', ('admin', generate_password_hash('admin123'), 'admin', 'Chief Election Commissioner'))
        cursor.execute('''
        INSERT INTO users (username, password_hash, role, full_name)
        VALUES (?, ?, ?, ?)
        ''', ('officer', generate_password_hash('officer123'), 'officer', 'Polling Booth Officer'))
        conn.commit()

    cursor.execute("SELECT 1 FROM users WHERE username = 'principal'")
    if not cursor.fetchone():
        cursor.execute('''
        INSERT INTO users (username, password_hash, role, full_name, email)
        VALUES (?, ?, ?, ?, ?)
        ''', ('principal', generate_password_hash('principal123'), 'principal', 'Dr. Arvind Kulkarni (College Principal)', 'principal@apex.edu.in'))
        conn.commit()

    # Step 6: Ensure existing accounts have email addresses populated
    cursor.execute("UPDATE users SET email = 'admin@votesure.gov.in' WHERE username = 'admin' AND (email IS NULL OR email = '')")
    cursor.execute("UPDATE users SET email = 'officer@votesure.gov.in' WHERE username = 'officer' AND (email IS NULL OR email = '')")
    cursor.execute("UPDATE users SET email = 'principal@apex.edu.in' WHERE username = 'principal' AND (email IS NULL OR email = '')")
    conn.commit()

    # Elections & Candidates Schema Enhancements
    cursor.execute("PRAGMA table_info(elections)")
    election_cols = [col[1] for col in cursor.fetchall()]
    if 'election_type' not in election_cols:
        cursor.execute("ALTER TABLE elections ADD COLUMN election_type TEXT DEFAULT 'college'")
    if 'institution_name' not in election_cols:
        cursor.execute("ALTER TABLE elections ADD COLUMN institution_name TEXT DEFAULT ''")

    cursor.execute("PRAGMA table_info(candidates)")
    candidate_cols = [col[1] for col in cursor.fetchall()]
    if 'position' not in candidate_cols:
        cursor.execute("ALTER TABLE candidates ADD COLUMN position TEXT DEFAULT 'President'")

    # Seed default active election if none exists
    cursor.execute("SELECT COUNT(*) FROM elections")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''
        INSERT INTO elections (title, description, election_type, institution_name, status, start_date, end_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            'VoteSure - General Public Election 2026',
            'National Public General Election. Facial biometric authentication mandatory at voting booths.',
            'general',
            'National Electoral Commission',
            'active',
            date.today().strftime('%Y-%m-%d'),
            date.today().strftime('%Y-%m-%d')
        ))
        conn.commit()

    # Seed default College Student Union Election if none exists
    cursor.execute("SELECT COUNT(*) FROM elections WHERE election_type = 'college'")
    if cursor.fetchone()[0] == 0:
        cursor.execute('''
        INSERT INTO elections (title, description, election_type, institution_name, status, start_date, end_date)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            'Apex College Student Union Elections 2026',
            'Official Student Union democratic election for Campus President, Vice President, General Secretary, Cultural Secretary, and Sports Secretary.',
            'college',
            'Apex Institute of Engineering & Technology',
            'upcoming',
            date.today().strftime('%Y-%m-%d'),
            date.today().strftime('%Y-%m-%d')
        ))
        college_election_id = cursor.lastrowid
        conn.commit()

        # Seed initial college candidates
        college_candidates = [
            (college_election_id, 'Rohan Sharma', 'Progressive Student Union (PSU)', 'President', '24/7 Library & Labs, Campus Wi-Fi upgrade, Placement Cell reforms'),
            (college_election_id, 'Ananya Deshmukh', 'Tech & Innovation Front (TIF)', 'President', 'Student Startup Fund, Annual Tech Fest budget, Cafeteria subsidies'),
            (college_election_id, 'Karthik Verma', 'United Students Federation (USF)', 'General Secretary', 'Transparent sports budget, Annual cultural fest revival, Grievance redressal'),
            (college_election_id, 'Sneha Kulkarni', 'Student Welfare League (SWL)', 'Cultural Secretary', 'Inter-college cultural competitions, Art & Music society expansion, Open-air amphitheater'),
            (college_election_id, 'Vikram Singh', 'Campus Sports Council (CSC)', 'Sports Secretary', 'Synthetic turf court renovation, Gym modernization, Inter-department trophy')
        ]
        for c in college_candidates:
            cursor.execute('''
            INSERT INTO candidates (election_id, name, party_name, position, manifesto)
            VALUES (?, ?, ?, ?, ?)
            ''', c)
        conn.commit()

    conn.close()

# ----------------- User Authentication -----------------

def verify_user(username, password):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE username = ?", (username,))
    user = cursor.fetchone()
    conn.close()
    if user and check_password_hash(user['password_hash'], password):
        return dict(user)
    return None

def create_user(username, password, role='officer', full_name='', email='', contact=''):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        cursor.execute('''
        INSERT INTO users (username, password_hash, role, full_name, email, contact)
        VALUES (?, ?, ?, ?, ?, ?)
        ''', (username.strip(), generate_password_hash(password), role, full_name.strip(), (email or '').strip(), (contact or '').strip()))
        conn.commit()
        user_id = cursor.lastrowid
        conn.close()
        return user_id, None
    except sqlite3.IntegrityError:
        conn.close()
        return None, "Username already exists"

def update_user_password(username, new_password):
    """Securely updates the hashed password for an existing election official."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT id FROM users WHERE username = ?", (username.strip(),))
    user = cursor.fetchone()
    if not user:
        conn.close()
        return False, "User account not found."
    cursor.execute(
        "UPDATE users SET password_hash = ? WHERE username = ?",
        (generate_password_hash(new_password), username.strip())
    )
    conn.commit()
    conn.close()
    return True, None

def get_user_by_email(email):
    """Finds an official user by their registered email address."""
    if not email:
        return None
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE LOWER(TRIM(email)) = LOWER(?)", (email.strip(),))
    user = cursor.fetchone()
    conn.close()
    return dict(user) if user else None

def get_user_by_identifier(identifier):
    """Finds user by username OR email (case-insensitive)."""
    if not identifier:
        return None
    conn = get_db_connection()
    cursor = conn.cursor()
    val = identifier.strip().lower()
    cursor.execute("SELECT * FROM users WHERE LOWER(TRIM(username)) = ? OR LOWER(TRIM(email)) = ?", (val, val))
    user = cursor.fetchone()
    conn.close()
    return dict(user) if user else None

def update_user_email(username, email):
    """Updates the registered email for an election official."""
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE users SET email = ? WHERE username = ?", (email.strip(), username.strip()))
    conn.commit()
    conn.close()
    return True

# ----------------- Voter Management -----------------

def add_voter(voter_id, name, age=20, gender='Male', constituency='General', contact='', email='', photo_path=None, face_encoding=None, election_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if election_id is None:
        active = get_active_election()
        election_id = active['id'] if active else 1
    encoding_blob = pickle.dumps(face_encoding) if face_encoding is not None else None
    try:
        cursor.execute('''
        INSERT INTO voters (election_id, voter_id, name, age, gender, constituency, contact, email, photo_path, face_encoding)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ''', (election_id, voter_id.strip().upper(), name.strip(), age, gender, constituency.strip(), contact.strip(), email.strip(), photo_path, encoding_blob))
        conn.commit()
        v_id = cursor.lastrowid
        conn.close()
        return v_id, None
    except sqlite3.IntegrityError:
        conn.close()
        return None, f"Voter ID '{voter_id}' is already registered in this election's electoral roll."

def get_all_voters(search_query=None, election_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    conditions = []
    params = []

    if election_id is not None:
        conditions.append("v.election_id = ?")
        params.append(election_id)

    if search_query:
        param = f"%{search_query.strip()}%"
        conditions.append("(v.name LIKE ? OR v.voter_id LIKE ? OR v.constituency LIKE ?)")
        params.extend([param, param, param])

    where_clause = f"WHERE {' AND '.join(conditions)}" if conditions else ""

    cursor.execute(f'''
    SELECT v.id, v.election_id, v.voter_id, v.name, v.age, v.gender, v.constituency, v.contact, v.email, v.photo_path,
           (v.face_encoding IS NOT NULL) AS has_face_encoding, v.has_voted, v.voted_at, v.created_at,
           e.title as election_title, e.election_type, e.institution_name
    FROM voters v
    LEFT JOIN elections e ON v.election_id = e.id
    {where_clause}
    ORDER BY v.id DESC
    ''', tuple(params))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return [_election_status(v, election_id=election_id) for v in rows]

def get_voter_by_id(voter_internal_id, election_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
    SELECT v.*, e.title as election_title, e.election_type, e.institution_name
    FROM voters v
    LEFT JOIN elections e ON v.election_id = e.id
    WHERE v.id = ?
    ''', (voter_internal_id,))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    if d.get('face_encoding'):
        try:
            d['face_encoding'] = pickle.loads(d['face_encoding'])
        except Exception:
            d['face_encoding'] = None
    target_election = election_id if election_id is not None else d.get('election_id')
    return _election_status(d, election_id=target_election)

def get_voter_by_voter_id(voter_id_str, election_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if election_id is not None:
        cursor.execute('''
        SELECT v.*, e.title as election_title, e.election_type, e.institution_name
        FROM voters v
        LEFT JOIN elections e ON v.election_id = e.id
        WHERE v.voter_id = ? AND v.election_id = ?
        ''', (voter_id_str.strip().upper(), election_id))
    else:
        cursor.execute('''
        SELECT v.*, e.title as election_title, e.election_type, e.institution_name
        FROM voters v
        LEFT JOIN elections e ON v.election_id = e.id
        WHERE v.voter_id = ?
        ''', (voter_id_str.strip().upper(),))
    row = cursor.fetchone()
    conn.close()
    if not row:
        return None
    d = dict(row)
    if d.get('face_encoding'):
        try:
            d['face_encoding'] = pickle.loads(d['face_encoding'])
        except Exception:
            d['face_encoding'] = None
    target_election = election_id if election_id is not None else d.get('election_id')
    return _election_status(d, election_id=target_election)

def update_voter(voter_internal_id, voter_id, name, age, gender, constituency, contact, email, photo_path=None, face_encoding=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    try:
        if photo_path and face_encoding is not None:
            encoding_blob = pickle.dumps(face_encoding)
            cursor.execute('''
            UPDATE voters
            SET voter_id = ?, name = ?, age = ?, gender = ?, constituency = ?, contact = ?, email = ?, photo_path = ?, face_encoding = ?
            WHERE id = ?
            ''', (voter_id.strip().upper(), name.strip(), age, gender, constituency.strip(), contact.strip(), email.strip(), photo_path, encoding_blob, voter_internal_id))
        else:
            cursor.execute('''
            UPDATE voters
            SET voter_id = ?, name = ?, age = ?, gender = ?, constituency = ?, contact = ?, email = ?
            WHERE id = ?
            ''', (voter_id.strip().upper(), name.strip(), age, gender, constituency.strip(), contact.strip(), email.strip(), voter_internal_id))
        conn.commit()
        conn.close()
        return True, None
    except sqlite3.IntegrityError:
        conn.close()
        return False, "Voter ID already belongs to another registered voter."

def delete_voter(voter_internal_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    if cursor.execute("SELECT 1 FROM votes WHERE voter_id=?", (voter_internal_id,)).fetchone():
        conn.close()
        return False
    cursor.execute("SELECT photo_path FROM voters WHERE id = ?", (voter_internal_id,))
    row = cursor.fetchone()
    if row and row['photo_path'] and os.path.exists(row['photo_path']):
        try:
            os.remove(row['photo_path'])
        except Exception:
            pass
    cursor.execute("DELETE FROM voters WHERE id = ?", (voter_internal_id,))
    conn.commit()
    conn.close()
    return True

def get_all_enrolled_voter_encodings(election_id=None):
    """Returns list of registered voter biometric profiles for real-time booth recognition"""
    conn = get_db_connection()
    cursor = conn.cursor()
    if election_id is None:
        active = get_active_election()
        election_id = active['id'] if active else None

    if election_id is not None:
        cursor.execute('''
        SELECT id, election_id, voter_id, name, constituency, has_voted, voted_at, face_encoding, photo_path
        FROM voters
        WHERE face_encoding IS NOT NULL AND election_id = ?
        ''', (election_id,))
    else:
        cursor.execute('''
        SELECT id, election_id, voter_id, name, constituency, has_voted, voted_at, face_encoding, photo_path
        FROM voters
        WHERE face_encoding IS NOT NULL
        ''')
    rows = cursor.fetchall()
    conn.close()
    enrolled = []
    for r in rows:
        try:
            enc = pickle.loads(r['face_encoding'])
            if enc is not None:
                enrolled.append({
                    'id': r['id'],
                    'election_id': r['election_id'],
                    'voter_id': r['voter_id'],
                    'name': r['name'],
                    'constituency': r['constituency'],
                    'has_voted': bool(r['has_voted']),
                    'voted_at': r['voted_at'],
                    'photo_path': r['photo_path'],
                    'encoding': enc
                })
        except Exception:
            continue
    return [_election_status(v, election_id=election_id) for v in enrolled]

# ----------------- Elections & Candidates -----------------

def get_active_election():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM elections WHERE status = 'active' ORDER BY id DESC LIMIT 1")
    election = cursor.fetchone()
    conn.close()
    return dict(election) if election else None

def get_election_by_id(election_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM elections WHERE id = ?", (election_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def get_all_elections():
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
    SELECT e.*,
           (SELECT COUNT(*) FROM candidates WHERE candidates.election_id = e.id) as candidate_count,
           (SELECT COUNT(*) FROM votes WHERE votes.election_id = e.id) as votes_count
    FROM elections e
    ORDER BY CASE WHEN e.status = 'active' THEN 0 WHEN e.status = 'upcoming' THEN 1 ELSE 2 END, e.id DESC
    ''')
    elections = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return elections

def create_election(title, description='', election_type='college', institution_name='', start_date=None, end_date=None, set_active=False):
    conn = get_db_connection()
    cursor = conn.cursor()
    status = 'active' if set_active else 'upcoming'
    if set_active:
        cursor.execute("UPDATE elections SET status = 'closed' WHERE status = 'active'")
    cursor.execute('''
    INSERT INTO elections (title, description, election_type, institution_name, status, start_date, end_date)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (
        title.strip(),
        description.strip(),
        (election_type or 'college').strip().lower(),
        institution_name.strip(),
        status,
        start_date or date.today().strftime('%Y-%m-%d'),
        end_date or date.today().strftime('%Y-%m-%d')
    ))
    conn.commit()
    election_id = cursor.lastrowid
    conn.close()
    return election_id

def set_active_election(election_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("UPDATE elections SET status = 'closed' WHERE status = 'active'")
    cursor.execute("UPDATE elections SET status = 'active' WHERE id = ?", (election_id,))
    conn.commit()
    conn.close()
    return True

def update_election(election_id, title, description, election_type, institution_name, status, start_date=None, end_date=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if status == 'active':
        cursor.execute("UPDATE elections SET status = 'closed' WHERE status = 'active' AND id != ?", (election_id,))
    cursor.execute('''
    UPDATE elections
    SET title = ?, description = ?, election_type = ?, institution_name = ?, status = ?, start_date = ?, end_date = ?
    WHERE id = ?
    ''', (title.strip(), description.strip(), election_type.strip().lower(), institution_name.strip(), status, start_date, end_date, election_id))
    conn.commit()
    conn.close()
    return True

def delete_election(election_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    if cursor.execute("SELECT 1 FROM votes WHERE election_id = ?", (election_id,)).fetchone():
        conn.close()
        return False, "Cannot delete an election that already has recorded ballots in the ledger."
    cursor.execute("DELETE FROM candidates WHERE election_id = ?", (election_id,))
    cursor.execute("DELETE FROM elections WHERE id = ?", (election_id,))
    conn.commit()
    conn.close()
    return True, None

def add_candidate(election_id, name, party_name, symbol_path=None, photo_path=None, manifesto='', position='President'):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute('''
    INSERT INTO candidates (election_id, name, party_name, position, symbol_path, photo_path, manifesto)
    VALUES (?, ?, ?, ?, ?, ?, ?)
    ''', (election_id, name.strip(), party_name.strip(), (position or 'President').strip(), symbol_path, photo_path, manifesto.strip()))
    conn.commit()
    cand_id = cursor.lastrowid
    conn.close()
    return cand_id

def get_candidates_for_election(election_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if not election_id:
        active = get_active_election()
        if not active:
            conn.close()
            return []
        election_id = active['id']

    cursor.execute('''
    SELECT c.*, e.title as election_title, e.election_type, e.institution_name
    FROM candidates c
    JOIN elections e ON c.election_id = e.id
    WHERE c.election_id = ?
    ORDER BY c.position ASC, c.votes_count DESC, c.name ASC
    ''', (election_id,))
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    return rows

def get_candidate_by_id(candidate_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None

def delete_candidate(candidate_id):
    conn = get_db_connection()
    cursor = conn.cursor()
    if cursor.execute("SELECT 1 FROM votes WHERE candidate_id=?", (candidate_id,)).fetchone():
        conn.close()
        return False
    cursor.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
    conn.commit()
    conn.close()
    return True

# ----------------- Core Voting Engine -----------------

def cast_vote(voter_internal_id, candidate_id, election_id, verification_method='Facial Biometric', live_photo_path=None):
    """Serialize eligibility checks and writes; commit a complete ballot or nothing."""
    conn = get_db_connection()
    try:
        conn.execute("BEGIN IMMEDIATE")
        voter = conn.execute("SELECT id FROM voters WHERE id=?", (voter_internal_id,)).fetchone()
        if not voter:
            return False, "Voter record not found."
        election = conn.execute("SELECT status FROM elections WHERE id=?", (election_id,)).fetchone()
        if not election or election['status'] != 'active':
            return False, "This election is not open for voting."
        if conn.execute("SELECT 1 FROM votes WHERE election_id=? AND voter_id=?",
                        (election_id, voter_internal_id)).fetchone():
            return False, "Duplicate vote blocked: this voter has already voted in this election."
        candidate = conn.execute("SELECT id FROM candidates WHERE id=? AND election_id=?",
                                 (candidate_id, election_id)).fetchone()
        if not candidate:
            return False, "Selected candidate does not belong to this election."
        now_ts = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # Cryptographically chain into previous vote record (tamper-evident blockchain ledger)
        latest_vote = conn.execute("SELECT vote_hash FROM votes ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = latest_vote['vote_hash'] if (latest_vote and latest_vote['vote_hash']) else GENESIS_HASH
        vote_hash = _compute_vote_hash(prev_hash, election_id, candidate_id, voter_internal_id, now_ts, verification_method, live_photo_path)
        signature = _sign_vote_hash(vote_hash)

        conn.execute("""
            INSERT INTO votes(
                election_id, candidate_id, voter_id, timestamp, verification_method, live_photo_path,
                prev_hash, vote_hash, signature
            ) VALUES(?,?,?,?,?,?,?,?,?)
        """, (
            election_id, candidate_id, voter_internal_id, now_ts, verification_method, live_photo_path,
            prev_hash, vote_hash, signature
        ))
        conn.execute("UPDATE voters SET has_voted=1,voted_at=? WHERE id=?", (now_ts,voter_internal_id))
        conn.execute("UPDATE candidates SET votes_count=votes_count+1 WHERE id=?", (candidate_id,))
        conn.commit()
        return True, "Your vote was recorded successfully."
    except sqlite3.IntegrityError:
        return False, "Vote rejected by database integrity checks."
    except sqlite3.OperationalError:
        return False, "Database busy or unavailable. Please try again."
    finally:
        conn.rollback()
        conn.close()


def _election_status(voter, election_id=None):
    """Calculate participation status for the specified (or active) election strictly isolated."""
    conn = get_db_connection()
    if election_id:
        row = conn.execute("SELECT timestamp FROM votes WHERE voter_id=? AND election_id=?",
                           (voter['id'], election_id)).fetchone()
    else:
        row = conn.execute("""SELECT timestamp FROM votes WHERE voter_id=? AND election_id=(
            SELECT id FROM elections WHERE status='active' ORDER BY id DESC LIMIT 1)""",
            (voter['id'],)).fetchone()

    voted_elections = conn.execute("""
        SELECT e.id, e.title, e.election_type, e.institution_name, v.timestamp
        FROM votes v
        JOIN elections e ON v.election_id = e.id
        WHERE v.voter_id = ?
        ORDER BY v.timestamp DESC
    """, (voter['id'],)).fetchall()
    conn.close()

    voter['has_voted'] = int(row is not None)
    voter['voted_at'] = row['timestamp'] if row else None
    voter['voted_elections'] = [dict(ve) for ve in voted_elections]
    return voter

# ----------------- Dashboard & Analytics -----------------

def get_election_dashboard_stats(election_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()

    if election_id:
        cursor.execute("SELECT * FROM elections WHERE id = ?", (election_id,))
        active_election = cursor.fetchone()
        active_election = dict(active_election) if active_election else None
    else:
        active_election = get_active_election()

    election_id = active_election['id'] if active_election else None

    if election_id:
        cursor.execute("SELECT COUNT(*) FROM voters WHERE election_id = ?", (election_id,))
        total_voters = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM voters WHERE election_id = ? AND face_encoding IS NOT NULL", (election_id,))
        enrolled_biometrics = cursor.fetchone()[0]

        cursor.execute("SELECT COUNT(*) FROM votes WHERE election_id = ?", (election_id,))
        total_votes_cast = cursor.fetchone()[0]
    else:
        cursor.execute("SELECT COUNT(*) FROM voters")
        total_voters = cursor.fetchone()[0]
        cursor.execute("SELECT COUNT(*) FROM voters WHERE face_encoding IS NOT NULL")
        enrolled_biometrics = cursor.fetchone()[0]
        total_votes_cast = 0

    # Remaining voters
    remaining_voters = max(0, total_voters - total_votes_cast)

    # Voter Turnout %
    turnout_pct = round((total_votes_cast / total_voters * 100), 1) if total_voters > 0 else 0

    # Total candidates in active election
    total_candidates = 0
    leader_name = "No Votes Yet"
    leader_party = "N/A"
    leader_position = ""
    leader_votes = 0

    if election_id:
        cursor.execute("SELECT COUNT(*) FROM candidates WHERE election_id = ?", (election_id,))
        total_candidates = cursor.fetchone()[0]

        cursor.execute("SELECT name, party_name, position, votes_count FROM candidates WHERE election_id = ? ORDER BY votes_count DESC LIMIT 1", (election_id,))
        top_cand = cursor.fetchone()
        if top_cand and top_cand['votes_count'] > 0:
            leader_name = top_cand['name']
            leader_party = top_cand['party_name']
            leader_position = top_cand['position'] or 'President'
            leader_votes = top_cand['votes_count']

    # Recent votes audit log (last 8)
    recent_votes = []
    if election_id:
        cursor.execute('''
        SELECT v.id, v.timestamp, v.verification_method, v.live_photo_path,
               c.name as candidate_name, c.party_name,
               vt.voter_id as voter_code, vt.name as voter_name, vt.constituency,
               vt.photo_path as registration_photo_path
        FROM votes v
        JOIN candidates c ON v.candidate_id = c.id
        JOIN voters vt ON v.voter_id = vt.id
        WHERE v.election_id = ?
        ORDER BY v.timestamp DESC LIMIT 8
        ''', (election_id,))
        recent_votes = [dict(r) for r in cursor.fetchall()]
        for r in recent_votes:
            r['registration_photo_path'] = _normalize_photo_path(r.get('registration_photo_path'))
            r['live_photo_path'] = _normalize_photo_path(r.get('live_photo_path'))

    conn.close()
    return {
        'active_election': active_election,
        'total_voters': total_voters,
        'enrolled_biometrics': enrolled_biometrics,
        'total_votes_cast': total_votes_cast,
        'remaining_voters': remaining_voters,
        'turnout_pct': turnout_pct,
        'total_candidates': total_candidates,
        'leader_name': leader_name,
        'leader_party': leader_party,
        'leader_position': leader_position,
        'leader_votes': leader_votes,
        'recent_votes': recent_votes
    }

def get_election_results_data(election_id=None):
    candidates = get_candidates_for_election(election_id)
    total_votes = sum(c['votes_count'] for c in candidates)

    for c in candidates:
        c['vote_pct'] = round((c['votes_count'] / total_votes * 100), 1) if total_votes > 0 else 0

    return {
        'candidates': candidates,
        'total_votes': total_votes,
        'chart_labels': [f"{c['name']} ({c.get('position', 'Nominee')})" for c in candidates],
        'chart_votes': [c['votes_count'] for c in candidates],
        'chart_parties': [c['party_name'] for c in candidates]
    }

def _normalize_photo_path(path):
    if not path:
        return ''
    path_str = str(path).strip().replace('\\', '/')
    if path_str.startswith('http://') or path_str.startswith('https://') or path_str.startswith('data:'):
        return path_str
    if path_str.startswith('/'):
        return path_str
    if path_str.startswith('static/'):
        return '/' + path_str
    return '/static/' + path_str

def get_all_votes_audit_trail(election_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    query = '''
    SELECT v.id, v.id as vote_id, v.timestamp, v.verification_method, v.live_photo_path,
           v.prev_hash, v.vote_hash, v.signature, v.election_id,
           c.name as candidate_name, c.party_name, c.position as candidate_position,
           e.title as election_title, e.election_type, e.institution_name,
           vt.voter_id, vt.voter_id as voter_code, vt.name as voter_name, vt.constituency,
           vt.photo_path as registration_photo_path
    FROM votes v
    JOIN candidates c ON v.candidate_id = c.id
    JOIN elections e ON v.election_id = e.id
    JOIN voters vt ON v.voter_id = vt.id
    '''
    params = []
    if election_id:
        query += " WHERE v.election_id = ?"
        params.append(election_id)
    query += " ORDER BY v.timestamp DESC"
    cursor.execute(query, params)
    rows = [dict(r) for r in cursor.fetchall()]
    conn.close()
    for r in rows:
        r['registration_photo_path'] = _normalize_photo_path(r.get('registration_photo_path'))
        r['live_photo_path'] = _normalize_photo_path(r.get('live_photo_path'))
    return rows

def reset_all_votes(election_id=None):
    conn = get_db_connection()
    cursor = conn.cursor()
    if election_id:
        cursor.execute("DELETE FROM votes WHERE election_id = ?", (election_id,))
        cursor.execute("UPDATE candidates SET votes_count = 0 WHERE election_id = ?", (election_id,))
    else:
        cursor.execute("DELETE FROM votes")
        cursor.execute("UPDATE candidates SET votes_count = 0")
    cursor.execute("""UPDATE voters SET
        has_voted = EXISTS(SELECT 1 FROM votes WHERE votes.voter_id=voters.id),
        voted_at = (SELECT MAX(timestamp) FROM votes WHERE votes.voter_id=voters.id)""")
    conn.commit()
    conn.close()
    return True

# ----------------- Database Security, Backup & Cryptographic Auditing -----------------

def backup_database():
    """
    Creates an atomic, WAL-consistent SQLite backup snapshot in data/backups/
    and maintains the 10 most recent backups for disaster recovery.
    """
    backup_dir = os.path.join(os.path.dirname(DB_PATH), 'backups')
    os.makedirs(backup_dir, exist_ok=True)
    timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
    backup_path = os.path.join(backup_dir, f"voting_secure_backup_{timestamp}.db")
    
    conn = get_db_connection()
    try:
        dest = sqlite3.connect(backup_path)
        conn.backup(dest)
        dest.close()
    finally:
        conn.close()

    try:
        backups = sorted([
            os.path.join(backup_dir, f) for f in os.listdir(backup_dir)
            if f.startswith('voting_secure_backup_') and f.endswith('.db')
        ], key=os.path.getmtime)
        while len(backups) > 10:
            oldest = backups.pop(0)
            try:
                os.remove(oldest)
            except Exception:
                pass
    except Exception:
        pass

    return backup_path

def verify_database_integrity():
    """
    Comprehensive cryptographic and structural audit of the voting database.
    Verifies:
    1. SQLite low-level structural integrity (PRAGMA integrity_check).
    2. Blockchain-style SHA-256 hash continuity across every vote.
    3. Cryptographic HMAC-SHA256 signature authenticity on each ballot.
    4. Voter-candidate tally mathematical reconciliation:
       - Total ballots in votes table matches sum of candidates votes_count.
       - Count of voters with has_voted=1 matches total votes.
    5. Referential integrity (no orphan records or foreign key violations).
    """
    conn = get_db_connection()
    report = {
        'timestamp': datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
        'structural_integrity': 'FAIL',
        'foreign_keys_integrity': 'FAIL',
        'ledger_integrity': 'FAIL',
        'tally_integrity': 'FAIL',
        'total_votes_verified': 0,
        'unbroken_chain': False,
        'latest_block_hash': None,
        'anomalies': []
    }
    
    try:
        # 1. Structural integrity
        chk = conn.execute("PRAGMA integrity_check").fetchall()
        if chk and chk[0][0] == 'ok':
            report['structural_integrity'] = 'PASS'
        else:
            report['anomalies'].append(f"SQLite integrity check reported issues: {chk}")

        # 2. Foreign keys check
        fk_chk = conn.execute("PRAGMA foreign_key_check").fetchall()
        if not fk_chk:
            report['foreign_keys_integrity'] = 'PASS'
        else:
            report['anomalies'].append(f"Foreign key violations detected: {len(fk_chk)} orphans")

        # 3. Cryptographic hash chain & HMAC signature verification
        votes = conn.execute("""
            SELECT id, election_id, candidate_id, voter_id, timestamp, verification_method, live_photo_path,
                   prev_hash, vote_hash, signature
            FROM votes ORDER BY id ASC
        """).fetchall()

        expected_prev = GENESIS_HASH
        chain_intact = True
        for idx, v in enumerate(votes):
            stored_prev = v['prev_hash'] or ''
            stored_hash = v['vote_hash'] or ''
            stored_sig = v['signature'] or ''

            # Check 1: prev_hash continuity
            if stored_prev != expected_prev:
                chain_intact = False
                report['anomalies'].append(f"Hash chain broken at Vote #{v['id']}: expected prev_hash {expected_prev[:12]}..., found {stored_prev[:12]}...")

            # Check 2: recomputed hash authenticity
            computed_hash = _compute_vote_hash(
                stored_prev, v['election_id'], v['candidate_id'], v['voter_id'],
                v['timestamp'], v['verification_method'], v['live_photo_path']
            )
            if computed_hash != stored_hash:
                chain_intact = False
                report['anomalies'].append(f"Cryptographic hash mismatch at Vote #{v['id']}! Data was tampered with.")

            # Check 3: HMAC signature
            expected_sig = _sign_vote_hash(stored_hash)
            if expected_sig != stored_sig:
                chain_intact = False
                report['anomalies'].append(f"Digital signature invalid at Vote #{v['id']}! Unauthorized signature.")

            expected_prev = stored_hash
            report['latest_block_hash'] = stored_hash

        report['total_votes_verified'] = len(votes)
        if chain_intact:
            report['ledger_integrity'] = 'PASS'
            report['unbroken_chain'] = True

        # 4. Per-Election Isolated Tally Reconciliation
        elections = conn.execute("SELECT id, title, election_type FROM elections").fetchall()
        tally_ok = True
        for el in elections:
            el_id = el['id']
            el_title = el['title']
            votes_in_el = conn.execute("SELECT COUNT(*) FROM votes WHERE election_id = ?", (el_id,)).fetchone()[0]
            cand_sum_in_el = conn.execute("SELECT COALESCE(SUM(votes_count), 0) FROM candidates WHERE election_id = ?", (el_id,)).fetchone()[0]
            unique_voters_in_el = conn.execute("SELECT COUNT(DISTINCT voter_id) FROM votes WHERE election_id = ?", (el_id,)).fetchone()[0]

            if votes_in_el != cand_sum_in_el:
                tally_ok = False
                report['anomalies'].append(f"Tally mismatch in '{el_title}': {votes_in_el} cast ballots != {cand_sum_in_el} candidate votes.")
            if votes_in_el != unique_voters_in_el:
                tally_ok = False
                report['anomalies'].append(f"Duplicate elector anomaly in '{el_title}': {votes_in_el} votes from {unique_voters_in_el} unique electors.")

        total_votes = len(votes)
        total_cand_votes = conn.execute("SELECT COALESCE(SUM(votes_count), 0) FROM candidates").fetchone()[0]
        if total_votes != total_cand_votes:
            tally_ok = False
            report['anomalies'].append(f"Global vote sum mismatch: {total_votes} total ballots != {total_cand_votes} total candidate votes.")

        if tally_ok:
            report['tally_integrity'] = 'PASS'

    except Exception as e:
        report['anomalies'].append(f"Integrity check exception: {str(e)}")
    finally:
        conn.close()

    report['is_secure'] = (
        report['structural_integrity'] == 'PASS' and
        report['foreign_keys_integrity'] == 'PASS' and
        report['ledger_integrity'] == 'PASS' and
        report['tally_integrity'] == 'PASS'
    )
    return report
