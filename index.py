import sqlite3
con = sqlite3.connect('bicc.db')
con.execute('CREATE INDEX IF NOT EXISTS idx_ie_eventdate ON interruption_events(event_date)')
con.execute('CREATE INDEX IF NOT EXISTS idx_ie_date_feeder ON interruption_events(event_date, feeder)')
con.execute('CREATE INDEX IF NOT EXISTS idx_sf_cc_div ON station_feeder(controlcenter, division)')
con.commit();
con.close();
print('Indexes created')
