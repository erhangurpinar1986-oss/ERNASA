"""Offline regression entry point: no production env or outbound connections."""
from pathlib import Path
import ipaddress
import os
import sys
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
STATE = ROOT/'.sales-test-state'/('offline-'+uuid4().hex)
STATE.mkdir(parents=True)
for key in list(os.environ):
    if key.startswith(('ERNASA_','RENDER_','OPENAI_','RESEND_','BREVO_','PG')) or key in ('DATABASE_URL','IK_SESSION_SECRET','SALES_TEST_POSTGRES_DSN'):
        os.environ.pop(key,None)
os.environ['ERNASA_IK_UPLOAD_DIRECTORY'] = str(STATE/'uploads')


def network_guard(event, arguments):
    if event == 'socket.connect':
        address = arguments[1]
        if isinstance(address,tuple):
            host = address[0]
            try:
                allowed = ipaddress.ip_address(host).is_loopback
            except ValueError:
                allowed = host == 'localhost'
            if not allowed:
                raise RuntimeError('Offline test attempted an outbound connection')


sys.addaudithook(network_guard)
sys.path.insert(0,str(ROOT))
import database
database.DATABASE_URL = None
database.DATABASE_PATH = STATE/'application.sqlite3'
import pytest
sys.exit(pytest.main(['tests','-q','--basetemp',str(STATE/'pytest'),*sys.argv[1:]]))
