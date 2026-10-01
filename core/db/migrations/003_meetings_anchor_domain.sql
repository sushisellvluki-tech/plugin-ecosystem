-- A meetings batch must reference a meetings Item, even under direct SQL.
ALTER TABLE meetings.batches ADD COLUMN domain text NOT NULL DEFAULT 'meetings' CHECK(domain='meetings');
ALTER TABLE meetings.batches ADD FOREIGN KEY(organization_id,domain,id)
 REFERENCES ecosystem.items(organization_id,domain,id);
