"""Run synthetic tests on the workspace-owned local cluster; never reset a DB.

This runner reads no production environment variables or credentials.
"""
from pathlib import Path
import os
import subprocess
import sys
import time

import psycopg2
from psycopg2 import sql

ROOT = Path(__file__).resolve().parents[1]
DATA = ROOT / '.sales-test-state' / 'pgdata'
BIN = ROOT.parent / 'postgresql-18.7-x64-portable' / 'pgsql' / 'bin'
DSN = 'host=127.0.0.1 port=55439 user=sales_test connect_timeout=2'
process = None
log = None


def verify(connection):
    with connection.cursor() as cursor:
        cursor.execute("SELECT current_setting('data_directory'),host(inet_server_addr()),inet_server_port(),current_user,current_database()")
        directory,host,port,user,database = cursor.fetchone()
        assert Path(directory).resolve() == DATA.resolve(), 'Unexpected cluster: refusing all writes'
        assert (host,port,user) == ('127.0.0.1',55439,'sales_test'), f'Unexpected endpoint identity: {host}, {port}, {user}'
        assert database in ('postgres','sales_test_hub')


try:
    assert DATA.is_dir() and (DATA/'PG_VERSION').read_text().strip() == '18'
    # No production settings are consulted; this executable uses only the fixed DATA.
    status = subprocess.run([str(BIN/'pg_ctl.exe'),'-D',str(DATA),'status'],capture_output=True)
    if status.returncode != 0:
        log = (ROOT/'.sales-test-state'/'postgres-runner.log').open('ab')
        process = subprocess.Popen([str(BIN/'postgres.exe'),'-D',str(DATA),'-h','127.0.0.1','-p','55439'],
                                   stdout=log,stderr=log,creationflags=getattr(subprocess,'CREATE_NO_WINDOW',0))
    connection = None
    for _ in range(40):
        try:
            connection = psycopg2.connect(DSN+' dbname=postgres')
            break
        except psycopg2.OperationalError:
            time.sleep(.25)
    if connection is None:
        raise RuntimeError('Local test cluster did not become ready')
    try:
        verify(connection)
        connection.rollback()  # End the read-only identity check before CREATE DATABASE.
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname='sales_test_hub'")
            exists = cursor.fetchone() is not None
            print('Verified workspace cluster at 127.0.0.1:55439; database exists:',exists,flush=True)
            if not exists:
                cursor.execute(sql.SQL('CREATE DATABASE {}').format(sql.Identifier('sales_test_hub')))
    finally:
        connection.close()
    connection = psycopg2.connect(DSN+' dbname=sales_test_hub')
    try:
        verify(connection)
        with connection.cursor() as cursor:
            cursor.execute("SELECT count(*) FROM information_schema.tables WHERE table_schema='ernasa_control'")
            print('Existing CONTROL table count in synthetic test DB:',cursor.fetchone()[0],flush=True)
    finally:
        connection.close()
    environment = dict(os.environ)
    for key in list(environment):
        if key.startswith(('ERNASA_','RENDER_')) or key in ('DATABASE_URL','PGPASSWORD','PGSERVICE','PGSERVICEFILE'):
            environment.pop(key,None)
    environment['SALES_TEST_POSTGRES_DSN'] = DSN+' dbname=sales_test_hub'
    result = subprocess.run([sys.executable,'-m','pytest','tests/test_sales_postgres.py','-q'],cwd=ROOT,env=environment)
    result_code = result.returncode
finally:
    if process is not None:
        subprocess.run([str(BIN/'pg_ctl.exe'),'-D',str(DATA),'-m','fast','-w','stop'],stdout=subprocess.DEVNULL)
        process.wait(timeout=15)
    if log:
        log.close()
sys.exit(result_code)
