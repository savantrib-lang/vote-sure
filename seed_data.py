import os
import sqlite3
import database
import face_engine

def seed_voting_data():
    database.init_db()

    # Get active election
    active = database.get_active_election()
    election_id = active['id'] if active else 1

    # 1. Seed Candidates if none exist
    candidates = database.get_candidates_for_election(election_id)
    if len(candidates) == 0:
        print("Seeding Election Candidates...")
        cands = [
            ("Rahul Patil", "Progressive Students Alliance (PSA)", "Student welfare, modern AI labs, and high-speed campus WiFi.", 12),
            ("Sneha Kulkarni", "Youth Tech Development Front (YTDF)", "Industry hackathons, technical symposiums, and coding bootcamps.", 19),
            ("Amit Deshmukh", "Campus Unity Party (CUP)", "Sports infrastructure, cultural festivals, and cafeteria upgrades.", 8),
            ("Pooja Nayak", "Innovation & Research Forum (IRF)", "Student startup incubators, tech research grants, and peer tutoring.", 15)
        ]
        for name, party, manifesto, votes in cands:
            database.add_candidate(
                election_id=election_id,
                name=name,
                party_name=party,
                manifesto=manifesto
            )
            # Update initial votes for demo visualization
            conn = database.get_db_connection()
            conn.execute("UPDATE candidates SET votes_count = ? WHERE name = ?", (votes, name))
            conn.commit()
            conn.close()

    # 2. Seed Registered Voters
    conn = database.get_db_connection()
    v_count = conn.execute("SELECT COUNT(*) FROM voters").fetchone()[0]
    conn.close()

    if v_count == 0:
        print("Seeding Electoral Roll (Voters)...")
        voters_sample = [
            ("TCE-VOT-2026-001", "Kiran Kumar", 21, "Male", "CSE Department - TCE Gadag", "+91 98451 00001", "kiran@tce.ac.in"),
            ("TCE-VOT-2026-002", "Darshan Bellad", 22, "Male", "CSE Department - TCE Gadag", "+91 98451 00002", "darshan@tce.ac.in"),
            ("TCE-VOT-2026-003", "Ananya Joshi", 20, "Female", "ISE Department - TCE Gadag", "+91 98451 00003", "ananya@tce.ac.in"),
            ("TCE-VOT-2026-004", "Vikram Hiremath", 22, "Male", "ECE Department - TCE Gadag", "+91 98451 00004", "vikram@tce.ac.in"),
            ("TCE-VOT-2026-005", "Kavya Angadi", 21, "Female", "CSE Department - TCE Gadag", "+91 98451 00005", "kavya@tce.ac.in"),
            ("TCE-VOT-2026-006", "Rohan Kulkarni", 20, "Male", "Civil Department - TCE Gadag", "+91 98451 00006", "rohan@tce.ac.in"),
            ("TCE-VOT-2026-007", "Megha Patil", 21, "Female", "Mech Department - TCE Gadag", "+91 98451 00007", "megha@tce.ac.in"),
            ("TCE-VOT-2026-008", "Abhishek Nayak", 22, "Male", "EEE Department - TCE Gadag", "+91 98451 00008", "abhishek@tce.ac.in"),
        ]

        # Check if we have an existing face photo from previous student registration
        existing_photo = None
        photo_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads', 'students')
        if os.path.exists(photo_dir):
            files = [f for f in os.listdir(photo_dir) if f.endswith('.jpg')]
            if files:
                existing_photo = os.path.join(photo_dir, files[0])

        existing_enc = None
        if existing_photo and os.path.exists(existing_photo):
            enc, crop, err = face_engine.extract_face_encoding(existing_photo)
            if not err:
                existing_enc = enc

        for idx, (vid, name, age, gender, const, phone, email) in enumerate(voters_sample):
            # Assign previously captured biometric face to enrolled voter for immediate testing!
            if idx == 0 and existing_enc is not None:
                database.add_voter(
                    voter_id=vid,
                    name=name,
                    age=age,
                    gender=gender,
                    constituency=const,
                    contact=phone,
                    email=email,
                    photo_path=f"uploads/students/{os.path.basename(existing_photo)}",
                    face_encoding=existing_enc
                )
            else:
                database.add_voter(
                    voter_id=vid,
                    name=name,
                    age=age,
                    gender=gender,
                    constituency=const,
                    contact=phone,
                    email=email
                )

    print("Voting system seeded successfully!")
    face_engine.reload_enrolled_voters()

if __name__ == '__main__':
    seed_voting_data()
