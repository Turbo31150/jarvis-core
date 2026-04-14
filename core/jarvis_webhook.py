#!/usr/bin/env python3
"""JARVIS Webhook Server — Reçoit events externes → publie sur Redis"""
from http.server import HTTPServer, BaseHTTPRequestHandler
import json, redis
from datetime import datetime

r = redis.Redis(decode_responses=True)

class WebhookHandler(BaseHTTPRequestHandler):
    def do_POST(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            data = json.loads(body)
            event_type = self.path.strip("/").replace("/","_") or "webhook"
            payload = json.dumps({
                "type": f"webhook_{event_type}",
                "data": data,
                "source": "external",
                "ts": datetime.now().isoformat()
            })
            r.publish("jarvis:events", payload)
            r.lpush("jarvis:event_log", payload)
            r.ltrim("jarvis:event_log", 0, 999)
            self.send_response(200)
            self.end_headers()
            self.wfile.write(b'{"ok":true}')
        except Exception as e:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(json.dumps({"error": str(e)}).encode())

    def log_message(self, *args): pass

if __name__ == "__main__":
    srv = HTTPServer(("0.0.0.0", 8766), WebhookHandler)
    print("JARVIS Webhook → http://0.0.0.0:8766/<event_type>")
    srv.serve_forever()
