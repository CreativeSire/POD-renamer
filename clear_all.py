import os
from sqlalchemy import create_engine, text
url = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
engine = create_engine(url)
conn = engine.connect()
conn.execute(text('TRUNCATE TABLE logs, batches RESTART IDENTITY CASCADE'))
conn.commit()

# Clear uploads folder
upload_dir = os.path.join(os.path.dirname(__file__), 'uploads')
if os.path.exists(upload_dir):
    for root, dirs, files in os.walk(upload_dir, topdown=False):
        for name in files:
            os.remove(os.path.join(root, name))
        for name in dirs:
            os.rmdir(os.path.join(root, name))
    print('CLEARED_DB_AND_STORAGE')
else:
    print('CLEARED_DB')
