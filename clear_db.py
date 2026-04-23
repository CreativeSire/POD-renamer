import os
from sqlalchemy import create_engine, text
url = os.environ.get('DATABASE_URL', '').replace('postgres://', 'postgresql://', 1)
engine = create_engine(url)
conn = engine.connect()
conn.execute(text('TRUNCATE TABLE logs, batches RESTART IDENTITY CASCADE'))
conn.commit()
conn.execute(text("UPDATE users SET role = 'user' WHERE username = 'Ewetu01'"))
conn.commit()
print('DONE')
