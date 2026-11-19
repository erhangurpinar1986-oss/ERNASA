"""Opt-in localhost UI preview using a fresh synthetic database per run."""
from contextlib import closing
from pathlib import Path
import sys
from threading import Thread
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from control import app, render_gateway, sales, sales_http
from fastapi import FastAPI, Request
import uvicorn

state = ROOT/'.sales-test-state'
state.mkdir(exist_ok=True)
path = state/('preview-'+uuid4().hex+'.sqlite3')
app.init(path)
password = 'Synthetic-preview-only-2026!'
with closing(app.connect(path)) as db, db:
    db.execute("INSERT INTO companies VALUES(1,'Sentetik ERNASA','Aktif')")
    db.execute("INSERT INTO users(id,email,name,secret,role) VALUES(1,'founder@example.test','Sentetik Kurucu',?,'founder')",(app.digest(password),))
    db.execute("INSERT INTO sales_accounts VALUES('preview-rep',1,'Sentetik Temsilci','rep@example.test',?,1)",(app.digest(password),))
    actor,founder = sales.Actor('preview-rep',1),sales.Actor('1',None,True)
    customer = sales.customer(db,actor,{'name':'Sentetik Teknoloji AŞ','domain':'preview.example.test'})['id']
    sales.classify(db,founder,{'id':customer,'classification':'new','reason':'Sentetik demo'})
    opportunity = sales.opportunity(db,actor,{'customer_id':customer,'title':'Sentetik İK aboneliği','kind':'subscription'})['id']
    proposal = sales.proposal(db,actor,{'opportunity_id':opportunity,'requested_minor':100000,'brief':'Sentetik ihtiyaç ve abonelik kapsamı'})['id']
    sales.submit(db,actor,{'id':proposal})
server = app.server(path,0)
server.public_prefix = '/control'
server.origin = 'http://127.0.0.1:18099'
Thread(target=server.serve_forever,daemon=True).start()
preview = FastAPI()


@preview.api_route('/control',methods=['GET','POST'])
@preview.api_route('/control/{rest:path}',methods=['GET','POST'])
async def forward(request: Request,rest: str=''):
    return await render_gateway.forward(request,server)


try:
    uvicorn.run(preview,host='127.0.0.1',port=18099,log_level='warning')
finally:
    server.shutdown()
    server.server_close()
