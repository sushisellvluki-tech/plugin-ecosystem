-- First-party meetings domain. Originals, drafts and check history are append-only.
CREATE SCHEMA meetings;
REVOKE ALL ON SCHEMA meetings FROM PUBLIC;
CREATE TABLE meetings.tenant_state (
 organization_id text PRIMARY KEY REFERENCES ecosystem.organizations(id),
 history_revision integer NOT NULL DEFAULT 0
);
CREATE TABLE meetings.batches (
 organization_id text NOT NULL, id uuid NOT NULL, source_account_id text NOT NULL,
 manifest_hash text NOT NULL, status text NOT NULL CHECK(status IN ('ingested','blocked_ingestion')),
 current_run_id uuid, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(organization_id,id),
 FOREIGN KEY(organization_id,id) REFERENCES ecosystem.items(organization_id,id)
);
CREATE TABLE meetings.sources (
 organization_id text NOT NULL REFERENCES ecosystem.organizations(id), id uuid NOT NULL,
 source_account_id text NOT NULL, external_key text NOT NULL,
 PRIMARY KEY(organization_id,id), UNIQUE(organization_id,source_account_id,external_key)
);
CREATE TABLE meetings.source_versions (
 organization_id text NOT NULL, id uuid NOT NULL, source_id uuid NOT NULL,
 revision integer NOT NULL CHECK(revision>0), sha256 text NOT NULL, text_content text NOT NULL,
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(organization_id,id),
 UNIQUE(organization_id,source_id,revision),
 FOREIGN KEY(organization_id,source_id) REFERENCES meetings.sources(organization_id,id)
);
CREATE INDEX meetings_source_hash ON meetings.source_versions(organization_id,source_id,sha256);
CREATE TABLE meetings.batch_entries (
 organization_id text NOT NULL, batch_id uuid NOT NULL, ordinal integer NOT NULL,
 filename text NOT NULL, format text NOT NULL, raw_text text NOT NULL, external_id text,
 status text NOT NULL CHECK(status IN ('accepted','duplicate','rejected')),
 reason text NOT NULL, source_version_id uuid,
 PRIMARY KEY(organization_id,batch_id,ordinal),
 FOREIGN KEY(organization_id,batch_id) REFERENCES meetings.batches(organization_id,id),
 FOREIGN KEY(organization_id,source_version_id) REFERENCES meetings.source_versions(organization_id,id)
);
CREATE TABLE meetings.runs (
 organization_id text NOT NULL, id uuid NOT NULL, batch_id uuid NOT NULL,
 artifact_hash text NOT NULL, source_hash text NOT NULL, claims jsonb NOT NULL,
 history_revision integer NOT NULL, rule_version text NOT NULL,
 created_by text NOT NULL, model_version text, prompt_version text,
 status text NOT NULL CHECK(status IN ('needs_review','failed','ready','published')),
 created_at timestamptz NOT NULL DEFAULT now(), PRIMARY KEY(organization_id,id),
 FOREIGN KEY(organization_id,batch_id) REFERENCES meetings.batches(organization_id,id),
 FOREIGN KEY(organization_id,created_by) REFERENCES ecosystem.memberships(organization_id,actor_id)
);
ALTER TABLE meetings.batches ADD FOREIGN KEY(organization_id,current_run_id) REFERENCES meetings.runs(organization_id,id);
CREATE TABLE meetings.check_results (
 organization_id text NOT NULL, run_id uuid NOT NULL, check_no integer NOT NULL CHECK(check_no BETWEEN 1 AND 4),
 revision integer NOT NULL CHECK(revision>0), verdict text NOT NULL CHECK(verdict IN ('pass','fail','needs_review')),
 reason text NOT NULL, method text NOT NULL CHECK(method IN ('deterministic','human')),
 rule_version text NOT NULL, source_hash text NOT NULL, artifact_hash text NOT NULL,
 reviewer_id text, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(organization_id,run_id,check_no,revision),
 FOREIGN KEY(organization_id,run_id) REFERENCES meetings.runs(organization_id,id),
 FOREIGN KEY(organization_id,reviewer_id) REFERENCES ecosystem.memberships(organization_id,actor_id)
);
CREATE TABLE meetings.publications (
 organization_id text NOT NULL, run_id uuid NOT NULL, history_revision integer NOT NULL,
 published_by text NOT NULL, created_at timestamptz NOT NULL DEFAULT now(),
 PRIMARY KEY(organization_id,run_id), UNIQUE(organization_id,history_revision),
 FOREIGN KEY(organization_id,run_id) REFERENCES meetings.runs(organization_id,id),
 FOREIGN KEY(organization_id,published_by) REFERENCES ecosystem.memberships(organization_id,actor_id)
);
DO $$ DECLARE t text; BEGIN
 FOREACH t IN ARRAY ARRAY['tenant_state','batches','sources','source_versions','batch_entries','runs','check_results','publications'] LOOP
  EXECUTE format('ALTER TABLE meetings.%I ENABLE ROW LEVEL SECURITY', t);
  EXECUTE format('ALTER TABLE meetings.%I FORCE ROW LEVEL SECURITY', t);
  EXECUTE format('CREATE POLICY tenant_domain ON meetings.%I USING (organization_id=current_setting(''app.organization_id'',true) AND current_setting(''app.domain'',true)=''meetings'') WITH CHECK (organization_id=current_setting(''app.organization_id'',true) AND current_setting(''app.domain'',true)=''meetings'')',t);
 END LOOP;
END $$;
