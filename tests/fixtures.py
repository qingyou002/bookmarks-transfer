import sqlite3
from pathlib import Path


def history(path: Path, urls=(), visits=(), *, extended=True):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE meta(key TEXT PRIMARY KEY, value TEXT);
        INSERT INTO meta VALUES('version','70');
        CREATE TABLE urls(id INTEGER PRIMARY KEY,url TEXT,title TEXT,visit_count INTEGER,
                          typed_count INTEGER,last_visit_time INTEGER,hidden INTEGER);
        CREATE TABLE visits(id INTEGER PRIMARY KEY,url INTEGER,visit_time INTEGER,
                            from_visit INTEGER,transition INTEGER,segment_id INTEGER,
                            visit_duration INTEGER,opener_visit INTEGER DEFAULT 0,
                            originator_cache_guid TEXT DEFAULT '',is_known_to_sync INTEGER DEFAULT 0,
                            visited_link_id INTEGER DEFAULT 0,
                            incremented_omnibox_typed_score INTEGER DEFAULT 0);
        CREATE TABLE visit_source(id INTEGER PRIMARY KEY,source INTEGER);
    ''')
    for row in urls:
        db.execute("INSERT INTO urls VALUES(?,?,?,?,?,?,?)", row)
    for row in visits:
        db.execute("INSERT INTO visits(id,url,visit_time,from_visit,transition,segment_id,visit_duration,opener_visit) "
                   "VALUES(?,?,?,?,?,?,?,?)", row)
    db.commit()
    db.close()


def addresses(path: Path, entries=()):
    path.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(path)
    db.executescript('''
        CREATE TABLE meta(key TEXT PRIMARY KEY,value TEXT);
        INSERT INTO meta VALUES('version','154');
        CREATE TABLE addresses(guid TEXT PRIMARY KEY,use_count INTEGER,use_date INTEGER,
          date_modified INTEGER,language_code TEXT,label TEXT,initial_creator_id INTEGER,
          record_type INTEGER);
        CREATE TABLE address_type_tokens(guid TEXT,type INTEGER,value TEXT,
          verification_status INTEGER,observations BLOB,PRIMARY KEY(guid,type));
        CREATE TABLE credit_cards(guid TEXT PRIMARY KEY,card_number_encrypted BLOB);
        INSERT INTO credit_cards VALUES('untouched',X'01020304');
        CREATE TABLE autofill_sync_metadata(model_type INTEGER,storage_key TEXT,value BLOB);
        INSERT INTO autofill_sync_metadata VALUES(1,'untouched',X'0506');
    ''')
    for guid, record_type, tokens in entries:
        db.execute("INSERT INTO addresses VALUES(?,2,123,123,'zh-CN','',99,?)", (guid, record_type))
        for field, value in tokens:
            db.execute("INSERT INTO address_type_tokens VALUES(?,?,?,4,X'')", (guid, field, value))
    db.commit()
    db.close()
