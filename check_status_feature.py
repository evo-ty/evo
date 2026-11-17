import os
import sys

sys.path.insert(0, os.path.dirname(__file__))
from main import create_app, init_db

init_db()
app = create_app()
client = app.test_client()

# Invalid public IDs must not expose server internals.
response = client.get('/server/not-a-public-id')
assert response.status_code == 404, response.status_code

# The status route remains reachable without authentication for a valid-looking ID.
response = client.get('/server/00000000')
assert response.status_code in (404, 503), response.status_code

print('status feature smoke test: PASS')
print('registered routes:', ', '.join(sorted(rule.rule for rule in app.url_map.iter_rules() if rule.rule.startswith('/server/'))))
