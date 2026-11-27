-- Additive, shared SQLite/PostgreSQL DDL; apply inside a transaction.
CREATE TABLE IF NOT EXISTS sales_accounts(
 id TEXT PRIMARY KEY, company_id BIGINT NOT NULL REFERENCES companies(id),
 name TEXT NOT NULL, email TEXT UNIQUE NOT NULL, secret TEXT NOT NULL,
 active INTEGER NOT NULL DEFAULT 0 CHECK(active IN (0,1)), UNIQUE(company_id,id));
CREATE TABLE IF NOT EXISTS sales_sessions(
 token TEXT PRIMARY KEY, account_id TEXT NOT NULL REFERENCES sales_accounts(id),
 csrf TEXT NOT NULL, expires BIGINT NOT NULL);
CREATE TABLE IF NOT EXISTS sales_customers(
 id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, owner_id TEXT NOT NULL,
 name TEXT NOT NULL, identity_key TEXT NOT NULL UNIQUE,
 classification TEXT NOT NULL DEFAULT 'unverified' CHECK(classification IN ('unverified','new','existing')),
 FOREIGN KEY(company_id,owner_id) REFERENCES sales_accounts(company_id,id), UNIQUE(company_id,owner_id,id));
CREATE TABLE IF NOT EXISTS sales_customer_identities(
 identity_key TEXT PRIMARY KEY, customer_id TEXT NOT NULL REFERENCES sales_customers(id));
CREATE TABLE IF NOT EXISTS sales_claims(
 id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, owner_id TEXT NOT NULL,
 customer_id TEXT NOT NULL REFERENCES sales_customers(id), requested_name TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','accepted','rejected')),
 reason TEXT NOT NULL DEFAULT '', FOREIGN KEY(company_id,owner_id) REFERENCES sales_accounts(company_id,id));
CREATE TABLE IF NOT EXISTS sales_opportunities(
 id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, owner_id TEXT NOT NULL,
 customer_id TEXT NOT NULL, title TEXT NOT NULL,
 kind TEXT NOT NULL CHECK(kind IN ('subscription','upgrade','addon','project')),
 FOREIGN KEY(company_id,owner_id,customer_id) REFERENCES sales_customers(company_id,owner_id,id),
 UNIQUE(company_id,owner_id,id));
CREATE TABLE IF NOT EXISTS sales_activities(
 id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, owner_id TEXT NOT NULL,
 opportunity_id TEXT NOT NULL, note TEXT NOT NULL, due_date TEXT NOT NULL DEFAULT '',
 FOREIGN KEY(company_id,owner_id,opportunity_id) REFERENCES sales_opportunities(company_id,owner_id,id));
CREATE TABLE IF NOT EXISTS sales_proposals(
 id TEXT PRIMARY KEY, company_id BIGINT NOT NULL, owner_id TEXT NOT NULL,
 opportunity_id TEXT NOT NULL, version INTEGER NOT NULL,
 status TEXT NOT NULL CHECK(status IN ('draft','submitted','revision','approved','rejected')),
 requested_minor BIGINT NOT NULL CHECK(requested_minor>0), official_minor BIGINT,
 discount_minor BIGINT NOT NULL DEFAULT 0 CHECK(discount_minor>=0), currency TEXT NOT NULL DEFAULT 'TRY' CHECK(currency='TRY'),
 brief TEXT NOT NULL, UNIQUE(opportunity_id,version),
 FOREIGN KEY(company_id,owner_id,opportunity_id) REFERENCES sales_opportunities(company_id,owner_id,id));
CREATE TABLE IF NOT EXISTS sales_decisions(
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES sales_proposals(id),
 actor_id BIGINT NOT NULL REFERENCES users(id), decision TEXT NOT NULL, reason TEXT NOT NULL, stamp TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sales_subscriptions(
 id TEXT PRIMARY KEY, opportunity_id TEXT UNIQUE NOT NULL REFERENCES sales_opportunities(id),
 customer_id TEXT NOT NULL REFERENCES sales_customers(id), eligible INTEGER NOT NULL CHECK(eligible IN (0,1)));
CREATE UNIQUE INDEX IF NOT EXISTS sales_first_product ON sales_subscriptions(customer_id) WHERE eligible=1;
CREATE TABLE IF NOT EXISTS sales_collections(
 id TEXT PRIMARY KEY, proposal_id TEXT NOT NULL REFERENCES sales_proposals(id),
 subscription_id TEXT REFERENCES sales_subscriptions(id), period INTEGER NOT NULL CHECK(period>0),
 reference TEXT NOT NULL UNIQUE, net_minor BIGINT NOT NULL CHECK(net_minor>0),
 cost_minor BIGINT NOT NULL DEFAULT 0 CHECK(cost_minor>=0), collected_on TEXT NOT NULL,
 verified_by BIGINT NOT NULL REFERENCES users(id), reason TEXT NOT NULL,
 held INTEGER NOT NULL DEFAULT 0 CHECK(held IN (0,1)), UNIQUE(subscription_id,period));
CREATE UNIQUE INDEX IF NOT EXISTS sales_single_project_collection ON sales_collections(proposal_id) WHERE subscription_id IS NULL;
CREATE TABLE IF NOT EXISTS sales_commissions(
 id TEXT PRIMARY KEY, collection_id TEXT UNIQUE NOT NULL REFERENCES sales_collections(id),
 amount_minor BIGINT NOT NULL CHECK(amount_minor>=0), rate INTEGER NOT NULL CHECK(rate IN (0,2,5,25)),
 status TEXT NOT NULL CHECK(status IN ('pending','approved','paid','reversed')),
 manual_reason TEXT NOT NULL DEFAULT '', approved_by BIGINT REFERENCES users(id), paid_on TEXT);
CREATE TABLE IF NOT EXISTS sales_adjustments(
 id TEXT PRIMARY KEY, commission_id TEXT UNIQUE NOT NULL REFERENCES sales_commissions(id),
 amount_minor BIGINT NOT NULL CHECK(amount_minor<=0), reason TEXT NOT NULL,
 actor_id BIGINT NOT NULL REFERENCES users(id), stamp TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS sales_audit(
 id TEXT PRIMARY KEY, company_id BIGINT, actor_id TEXT NOT NULL,
 event TEXT NOT NULL, target TEXT NOT NULL, detail TEXT NOT NULL, stamp TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS sales_owner_customers ON sales_customers(company_id,owner_id);
CREATE INDEX IF NOT EXISTS sales_owner_opportunities ON sales_opportunities(company_id,owner_id);
