"""Usage: python manage_db.py init|status|backup|schema|password [--username admin]"""
import argparse, getpass, sqlite3
from pathlib import Path
from datetime import datetime
import database
from werkzeug.security import generate_password_hash
p=argparse.ArgumentParser(description=__doc__)
p.add_argument('command',choices=['init','status','backup','schema','password'])
p.add_argument('--username',default='admin')
a=p.parse_args()
if a.command=='init':
    database.init_db()
    print('Database ready:',database.DB_PATH)
else:
    c=database.get_db_connection()
    if a.command=='status':
        print('Integrity:',c.execute('PRAGMA integrity_check').fetchone()[0])
        print('Foreign key violations:',len(c.execute('PRAGMA foreign_key_check').fetchall()))
        for t in ['users','voters','elections','candidates','votes']:
            print(t,c.execute('SELECT COUNT(*) FROM '+t).fetchone()[0])
    elif a.command=='backup':
        folder=Path(database.DB_PATH).parent/'backups';folder.mkdir(exist_ok=True)
        target=folder/('voting_'+datetime.now().strftime('%Y%m%d_%H%M%S_%f')+'.db')
        dest=sqlite3.connect(str(target));c.backup(dest);dest.close();print(target)
    elif a.command=='schema':
        rows=c.execute("SELECT sql FROM sqlite_master WHERE sql IS NOT NULL AND name NOT LIKE 'sqlite_%' ORDER BY type='trigger',type='index',name").fetchall()
        target=Path(__file__).resolve().parent/'schema.sql'
        target.write_text('-- Structure only. No personal data.\nPRAGMA foreign_keys=ON;\n\n'+'\n\n'.join(r[0]+';' for r in rows));print(target)
    elif a.command=='password':
        password=getpass.getpass('New password (12+ characters): ')
        if len(password)<12 or password!=getpass.getpass('Confirm: '):raise SystemExit('Passwords must match and have 12+ characters.')
        with c:result=c.execute('UPDATE users SET password_hash=? WHERE username=?',(generate_password_hash(password),a.username))
        print('Password updated.' if result.rowcount else 'Username not found.')
    c.close()
