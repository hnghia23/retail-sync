from sqlalchemy import create_engine, text

def get_engine():
    DB_USER = 'admin'
    DB_PASS = 'admin123'
    DB_HOST = 'localhost'   
    DB_PORT = '5433'
    DB_NAME = 'my_database'
    return create_engine(
        f'postgresql+psycopg2://{DB_USER}:{DB_PASS}@{DB_HOST}:{DB_PORT}/{DB_NAME}'
    )

engine = get_engine()
with engine.connect() as conn:
    result = conn.execute(text('SELECT version();'))
    for row in result:
        print(row)