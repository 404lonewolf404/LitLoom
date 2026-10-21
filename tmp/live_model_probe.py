import json,time,app
started=time.perf_counter()
result=app.model_json_request("Return only JSON with keys status and answer. Do not explain.", {"task":"ping","answer":"one word"}, timeout=30, max_output_tokens=120, reasoning_effort="low")
print(json.dumps({"elapsed_ms":round((time.perf_counter()-started)*1000,2),"result":result},ensure_ascii=False))
