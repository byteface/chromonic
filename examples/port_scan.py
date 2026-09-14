"""
Chromonic Native Network & Port Scanner
Demonstrates native Python socket execution tied directly to a live UI.
"""

import socket
# import time
from chromonic import App
from domonic.html import *

import time

root = body(
    div(
        div(
            h2("Network Node Inspector", _style="margin: 0; font-size: 20px; color: #f8fafc;"),
            p("Probing native OS sockets directly via Python background event loops.", _style="margin: 4px 0 0 0; font-size: 13px; color: #94a3b8;"),
            _style="margin-bottom: 24px; border-bottom: 1px solid #334155; padding-bottom: 16px;"
        ),
        div(
            input(_id="host-input", _value="8.8.8.8", _placeholder="Hostname / IP", _style="padding: 10px; background: #0f172a; border: 1px solid #334155; border-radius: 6px; color: #f8fafc; font-family: monospace; width: 60%;"),
            button("Scan Host", _id="scan-btn", _style="padding: 10px 20px; background: #3b82f6; color: white; border: none; border-radius: 6px; font-weight: 600; cursor: pointer; margin-left: 8px;"),
            _style="margin-bottom: 20px;"
        ),
        div(_id="results-container", _style="display: grid; grid-template-columns: 1fr 1fr; gap: 12px;"),
        _style="max-width: 600px; margin: 40px auto; padding: 28px; background: #1e293b; border-radius: 12px; box-shadow: 0 20px 25px -5px rgba(0,0,0,0.5); font-family: system-ui, sans-serif; color: white;"
    ),
    _style="background: #0f172a; height: 100vh; margin: 0; padding: 20px; box-sizing: border-box;"
)

app = App(root, width=700, height=550)

# Critical infrastructure ports to audit
TARGET_PORTS = [
    (21, "FTP"),
    (22, "SSH"),
    (80, "HTTP"),
    (443, "HTTPS"),
    (3306, "MySQL"),
    (5432, "PostgreSQL"),
    (6379, "Redis"),
    (8080, "Alt-HTTP")
]

def check_port(host, port):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.4)
    start = time.time()
    try:
        s.connect((host, port))
        latency = (time.time() - start) * 1000
        s.close()
        return True, f"{latency:.1f}ms"
    except Exception:
        return False, "Closed / Timeout"

@app.click("#scan-btn")
def run_port_scan(event):
    host = app.document.querySelector("#host-input").value.strip()
    container = app.document.querySelector("#results-container")
    container.innerHTML = "" # Clear previous
    
    for port, label in TARGET_PORTS:
        is_open, detail = check_port(host, port)
        
        status_color = "#22c55e" if is_open else "#64748b"
        status_badge = "OPEN" if is_open else "CLOSED"
        badge_bg = "rgba(34, 197, 94, 0.1)" if is_open else "rgba(100, 116, 139, 0.1)"
        
        card = div(
            div(
                span(f"{label} ", _style="font-weight: 600; font-size: 14px;"),
                span(f"({port})", _style="color: #64748b; font-size: 12px; font-family: monospace;"),
            ),
            div(
                span(detail, _style="font-size: 11px; color: #94a3b8; margin-right: 8px; font-family: monospace;"),
                span(status_badge, _style=f"color: {status_color}; background: {badge_bg}; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: bold;"),
                _style="display: flex; align-items: center;"
            ),
            _style="display: flex; justify-content: space-between; align-items: center; padding: 12px; background: #0f172a; border: 1px solid #334155; border-radius: 8px;"
        )
        container.appendChild(card)

if __name__ == "__main__":
    app.run()