# Smart Voting System — database setup

## What is included
Your website was already a Python Flask application connected to SQLite. The database is **data/voting.db** and database operations are in **database.py**. No MySQL server, XAMPP, cloud account or database password is required. The database stays on the computer running Flask; it is not browser storage.

The delivered database preserves 5 user accounts, 2 voters, 1 candidate, 1 election and 0 votes from your upload. A pre-migration database copy is in data/backups. Existing uploaded photos and face data are retained. Keep this ZIP private.

## Start on Windows
1. Extract the complete ZIP. Open the extracted thiranex folder.
2. Install 64-bit Python (Python 3.10 or 3.11 is a practical starting point for the existing face-recognition dependencies). In Command Prompt in that folder run:

```bat
python -m venv .venv
.venv\Scripts\activate
python -m pip install --upgrade pip
python -m pip install -r requirements.txt
python manage_db.py init
python manage_db.py status
python app.py
```

3. Keep the terminal open. Visit **http://127.0.0.1:5000** on the same laptop. Allow browser camera permission. Do not open the HTML files directly or use VS Code Live Server; the templates require Flask.
4. Use your existing login. For databases retaining the original demonstration credentials, admin / admin123 and officer / officer123 are the original defaults. Changed account passwords remain unchanged.
5. Change the demonstration passwords:

```bat
python manage_db.py password --username admin
python manage_db.py password --username officer
```

After installation you can double-click start_app.bat. If installation fails, retain the exact error: the existing dlib/face-recognition dependencies require compatible platform packages. Their installation and your physical webcam were not validated in this environment. Do not remove face recognition imports to bypass verification.

## Using the website
- Candidates: add your candidate names and groups.
- Voters: register each voter with a webcam capture.
- Booth: verify a registered face, then open the ballot and submit within two minutes.
- Results and Audit: view the stored demonstration records.
- New polling-officer accounts can be created at /register while signed in as admin. Public creation of privileged accounts has been disabled.
- Vote eligibility is checked per election. Election status is authoritative; the existing start_date/end_date fields remain descriptive and do not automatically open or close voting. The existing application has no election-management screen.

## Database tables
| Table | Stored information |
|---|---|
| users | Account name, password hash, role, full name |
| elections | Title, description, status and dates |
| candidates | Election, candidate details and cached vote count |
| voters | Voter ID, personal details, photo path, face encoding and legacy vote-status fields |
| votes | Election, candidate, voter and verification timestamp |

schema.sql is a structure-only export for your project report or a fresh SQLite database. Existing users, biometric records and photos are not in this SQL file. Do not import schema.sql over the existing database. Running manage_db.py init is the supported idempotent setup/migration command.

The votes table has UNIQUE(election_id, voter_id). Voting now uses BEGIN IMMEDIATE so eligibility checks, ballot insertion, voter state and candidate totals commit as one transaction. Candidate/election matching and active status are validated. Additional database triggers reject invalid ballots and protect records that have votes. The website calculates voter participation from the selected active election rather than treating one historical vote as a permanent lock.

## Backup and inspect
```bat
python manage_db.py backup
python manage_db.py status
python manage_db.py schema
```
Backup uses SQLite's online backup API. It copies the database only: also copy static/uploads and the source project for a complete recoverable installation. Stop the app before restoring a selected backup to data/voting.db. Do not delete this file to fix a login or dependency error. The demo reset button intentionally erases votes; back up first. Do not run seed_data.py against your working database.

A local SQLite database viewer can open data/voting.db to inspect tables. Avoid manual writes while the application runs: counters are maintained by the application transaction. Optional VOTING_DB_PATH overrides the database location; the default is project-relative and independent of the terminal working directory.

## Repairs and tests
- Fixed missing time import that caused verification/authorization errors.
- Added serialized vote transactions, closed-election checks and candidate/election validation.
- Made participation checks election-specific and corrected scoped vote resets.
- Prevented deletion of candidates/voters with recorded ballots.
- Supported JSON and form ballot requests and rejected malformed numeric IDs.
- Bound ballots to the verified election and a two-minute verification window; consumed verification after submission.
- Replaced the hard-coded session key with a locally generated secret (or SECRET_KEY environment setting), protected camera endpoints with login, restricted officer account creation to admins, and randomized new photo filenames.
- Corrected launcher labels and added backup, schema, status and password commands.

Run `python -m unittest -v test_backend`. Five automated tests passed: concurrent duplicate submissions; closed/mismatched elections; per-election participation and reset; repeatable migration and integrity; Flask pages, authorization and JSON ballot submission. Tests use isolated temporary databases. Vision modules are mocked only in the tests; no authentication bypass is added to the application. Physical camera capture, biometric matching, anti-spoofing effectiveness, and Windows dependency installation require local testing.

## Academic-prototype limitations
This update preserves your existing data model and UI. Votes still link voter identity directly to candidate choice, and the audit pages expose that link: this is not a secret-ballot system. Face data is stored using the existing pickle format and is not encrypted; never load a database from an untrusted source. Face-recognition and liveness accuracy are unverified, and rejecting file uploads cannot prove a submitted image came from a live camera. Real elections need a separate privacy/security design, independent audit and reliable liveness evaluation. Keep this demonstration on localhost; this package is not a production deployment.

Legacy attendance files remain for preservation but are not part of the voting database. Use app.py, not gui_app.py or the old attendance scripts.
