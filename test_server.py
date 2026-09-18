"""Test server to capture Codex CLI requests"""
import http.server
import json
import sys

class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        print(f"GET {self.path}", flush=True)
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(b'{"object":"list","data":[]}')
    
    def do_POST(self):
        length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(length)
        print(f"=== POST {self.path} ===", flush=True)
        print(f"Headers: {dict(self.headers)}", flush=True)
        body_str = body.decode('utf-8', 'replace')
        print(f"Body: {body_str[:2000]}", flush=True)
        # Save to file for inspection
        with open('last_request.json', 'w', encoding='utf-8') as f:
            f.write(body_str)
        # Return a minimal valid response
        resp = {
            "id": "resp_test",
            "object": "response",
            "created": 1700000000,
            "model": "deepseek-v4-flash",
            "output": [{
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "Hello!"}]
            }]
        }
        self.send_response(200)
        self.send_header('Content-Type', 'application/json')
        self.end_headers()
        self.wfile.write(json.dumps(resp).encode())
    
    def log_message(self, *a):
        pass

httpd = http.server.HTTPServer(('127.0.0.1', 9999), Handler)
print("Listening on 127.0.0.1:9999", flush=True)
httpd.handle_request()
httpd.handle_request()
httpd.handle_request()