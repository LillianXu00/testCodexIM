#!/usr/bin/env python3
"""Small stdlib bridge for hosts with shell tools but no HTTP MCP support."""
import json
import os
import sys
import urllib.request
import urllib.error

if len(sys.argv) != 3:
    raise SystemExit('Usage: planetbaltan_tool.py TOOL_NAME JSON_ARGUMENTS')
from urllib.parse import quote
url = os.environ.get('SIDECAR_URL', 'http://127.0.0.1:9101') + '/api/tools/' + quote(sys.argv[1], safe='')
request = urllib.request.Request(url, data=json.dumps(json.loads(sys.argv[2])).encode(),
    headers={'Content-Type': 'application/json', 'Authorization': 'Bearer ' + os.environ['SIDECAR_TOKEN']})
try:
    with urllib.request.urlopen(request, timeout=60) as response:
        print(response.read().decode())
except urllib.error.HTTPError as exc:
    print(exc.read().decode(), file=sys.stderr)
    raise SystemExit(1)
