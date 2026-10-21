import app,json
with app.db_session() as db:
 rows=db.execute("SELECT id,run_id,record_json FROM discovery_candidates WHERE json_extract(record_json,'$.intent_key')=? ORDER BY created_at DESC LIMIT 80",('intent:bf566e829748cefcb734dfd07b207b39',)).fetchall()
 print('rows',len(rows))
 for r in rows[:40]:
  x=json.loads(r['record_json']); print(r['run_id'][:8], x.get('title','')[:60], bool(x.get('abstract')), x.get('relevance_score'), x.get('model_relevance_label'), x.get('selection_backfilled'), x.get('seen_before'), x.get('user_decision'))
