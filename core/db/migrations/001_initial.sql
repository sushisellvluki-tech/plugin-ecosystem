CREATE SCHEMA ecosystem;
REVOKE ALL ON SCHEMA ecosystem FROM PUBLIC;
CREATE TABLE ecosystem.organizations (
 id text PRIMARY KEY CHECK (id <> ''), name text NOT NULL
);
CREATE TABLE ecosystem.memberships (
 organization_id text NOT NULL REFERENCES ecosystem.organizations(id),
 actor_id text NOT NULL CHECK (actor_id <> ''), scopes text[] NOT NULL DEFAULT '{}',
 active boolean NOT NULL DEFAULT true, PRIMARY KEY (organization_id, actor_id)
);
CREATE TABLE ecosystem.organization_domains (
 organization_id text NOT NULL REFERENCES ecosystem.organizations(id),
 domain text NOT NULL, enabled boolean NOT NULL DEFAULT true,
 PRIMARY KEY (organization_id, domain)
);
CREATE TABLE ecosystem.items (
 organization_id text NOT NULL REFERENCES ecosystem.organizations(id),
 id uuid NOT NULL, domain text NOT NULL, kind text NOT NULL,
 status text NOT NULL DEFAULT 'ingested' CHECK (status IN ('ingested','needs_review','archived')),
 revision integer NOT NULL DEFAULT 1 CHECK (revision > 0),
 meta jsonb NOT NULL DEFAULT '{}' CHECK (jsonb_typeof(meta) = 'object'),
 created_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 updated_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (organization_id,id), UNIQUE (organization_id,domain,id),
 FOREIGN KEY (organization_id,created_by) REFERENCES ecosystem.memberships(organization_id,actor_id),
 FOREIGN KEY (organization_id,domain) REFERENCES ecosystem.organization_domains(organization_id,domain)
);
CREATE INDEX items_lookup ON ecosystem.items(organization_id,domain,kind,status,id);
CREATE TABLE ecosystem.events (
 organization_id text NOT NULL, id uuid NOT NULL, domain text NOT NULL,
 kind text NOT NULL, item_id uuid NOT NULL, item_revision integer NOT NULL,
 actor_id text NOT NULL, request_id uuid NOT NULL,
 payload jsonb NOT NULL DEFAULT '{}', occurred_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (organization_id,id), UNIQUE(organization_id,domain,id),
 FOREIGN KEY (organization_id,domain,item_id) REFERENCES ecosystem.items(organization_id,domain,id),
 FOREIGN KEY (organization_id,actor_id) REFERENCES ecosystem.memberships(organization_id,actor_id)
);
CREATE INDEX events_item ON ecosystem.events(organization_id,item_id,item_revision);
CREATE TABLE ecosystem.idempotency (
 organization_id text NOT NULL REFERENCES ecosystem.organizations(id), domain text NOT NULL,
 operation text NOT NULL, key text NOT NULL CHECK (length(key) BETWEEN 1 AND 200),
 actor_id text NOT NULL, arguments_hash text NOT NULL, result jsonb,
 created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY (organization_id,operation,key),
 FOREIGN KEY (organization_id,actor_id) REFERENCES ecosystem.memberships(organization_id,actor_id)
);
CREATE TABLE ecosystem.outbox (
 organization_id text NOT NULL, id uuid NOT NULL, domain text NOT NULL, event_id uuid NOT NULL,
 attempts integer NOT NULL DEFAULT 0, available_at timestamptz NOT NULL DEFAULT now(),
 lease_token uuid, lease_until timestamptz, delivered_at timestamptz, dead_at timestamptz,
 PRIMARY KEY(organization_id,id), UNIQUE(organization_id,event_id),
 FOREIGN KEY(organization_id,domain,event_id) REFERENCES ecosystem.events(organization_id,domain,id),
 CHECK ((lease_token IS NULL) = (lease_until IS NULL))
);
CREATE INDEX outbox_pending ON ecosystem.outbox(organization_id,domain,available_at) WHERE delivered_at IS NULL AND dead_at IS NULL;
CREATE TABLE ecosystem.audit_log (
 organization_id text NOT NULL REFERENCES ecosystem.organizations(id), id uuid NOT NULL,
 domain text NOT NULL, actor_id text NOT NULL, request_id uuid NOT NULL,
 operation text NOT NULL, replayed boolean NOT NULL,
 occurred_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(organization_id,id)
);
-- Defaults deny reads/writes if transaction-local tenant/domain settings are absent.
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['organizations','memberships','organization_domains','items','events','idempotency','outbox','audit_log'] LOOP
  EXECUTE format('ALTER TABLE ecosystem.%I ENABLE ROW LEVEL SECURITY', t);
  EXECUTE format('ALTER TABLE ecosystem.%I FORCE ROW LEVEL SECURITY', t);
 END LOOP;
END $$;
CREATE POLICY tenant ON ecosystem.organizations USING (id = current_setting('app.organization_id',true));
CREATE POLICY tenant_actor ON ecosystem.memberships USING (
 organization_id=current_setting('app.organization_id',true) AND actor_id=current_setting('app.actor_id',true));
CREATE POLICY tenant ON ecosystem.organization_domains USING (organization_id=current_setting('app.organization_id',true));
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['items','events','idempotency','outbox','audit_log'] LOOP
  EXECUTE format('CREATE POLICY tenant_domain ON ecosystem.%I USING (organization_id=current_setting(''app.organization_id'',true) AND domain=current_setting(''app.domain'',true)) WITH CHECK (organization_id=current_setting(''app.organization_id'',true) AND domain=current_setting(''app.domain'',true))', t);
 END LOOP;
END $$;
