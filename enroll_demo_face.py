import cv2
import sys
import os
import face_engine
import database

def enroll_from_webcam(student_roll="2TG21CS001", student_name="Rahul Patil"):
    print(f"Opening webcam to capture face for {student_name} ({student_roll})...")
    cap = cv2.VideoCapture(0, cv2.CAP_DSHOW)
    if not cap.isOpened():
        cap = cv2.VideoCapture(0)
    
    if not cap.isOpened():
        print("Error: Could not access webcam.")
        return

    print("Webcam active! Look at the camera.")
    print("Press SPACEBAR to capture photo, or 'q' to quit.")

    captured_frame = None
    while True:
        ret, frame = cap.read()
        if not ret:
            break
        
        display_frame = frame.copy()
        cv2.putText(display_frame, f"Capturing: {student_name}", (20, 30),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
        cv2.putText(display_frame, "Press SPACE to Capture | Q to Cancel", (20, 65),
                    cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1)

        cv2.imshow("Enroll Face - TCE Gadag", display_frame)
        key = cv2.waitKey(1) & 0xFF
        if key == 32: # SPACE
            captured_frame = frame
            break
        elif key == ord('q'):
            break

    cap.release()
    cv2.destroyAllWindows()

    if captured_frame is not None:
        print("Extracting 128-d facial encodings...")
        enc, crop, err = face_engine.extract_face_encoding(captured_frame)
        if err:
            print(f"Failed: {err}")
            return
        
        save_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'static', 'uploads', 'students')
        os.makedirs(save_dir, exist_ok=True)
        photo_filename = f"{student_roll}_webcam.jpg"
        save_path = os.path.join(save_dir, photo_filename)
        cv2.imwrite(save_path, crop if crop is not None else captured_frame)
        rel_path = f"uploads/students/{photo_filename}"

        # Find student by roll
        conn = database.get_db_connection()
        stud = conn.execute("SELECT id FROM students WHERE roll_number = ?", (student_roll,)).fetchone()
        conn.close()

        if stud:
            database.update_student(
                student_id=stud['id'],
                name=student_name,
                roll_number=student_roll,
                class_section="CSE-6A",
                contact="+91 98451 23451",
                email="rahul.patil@tce.ac.in",
                photo_path=rel_path,
                face_encoding=enc
            )
            face_engine.reload_enrolled_faces()
            print(f"Success! Face enrolled for {student_name} ({student_roll}).")
        else:
            student_id, err = database.add_student(
                name=student_name,
                roll_number=student_roll,
                class_section="CSE-6A",
                contact="+91 98451 23451",
                email="rahul.patil@tce.ac.in",
                photo_path=rel_path,
                face_encoding=enc
            )
            face_engine.reload_enrolled_faces()
            print(f"Success! Added student and enrolled face (ID: {student_id}).")

if __name__ == '__main__':
    enroll_from_webcam()
