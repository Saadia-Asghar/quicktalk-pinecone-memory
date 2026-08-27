import base64, json, subprocess, time, urllib.request, uuid
from pathlib import Path
import websocket

CHROME = r"C:\Program Files\Google\Chrome\Application\chrome.exe"
HTML = Path("deliverables/QuickTalk_Internship_Presentation.html").resolve().as_uri()
PORT = 9333
profile = Path("deliverables/.chrome-qa").resolve()
proc = subprocess.Popen([
    CHROME, "--headless=new", "--disable-gpu", "--no-first-run",
    "--remote-allow-origins=*", f"--remote-debugging-port={PORT}",
    f"--user-data-dir={profile}", HTML,
], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

def endpoint():
    for _ in range(60):
        try:
            tabs = json.load(urllib.request.urlopen(f"http://127.0.0.1:{PORT}/json", timeout=1))
            page = next(x for x in tabs if x.get("type") == "page")
            return page["webSocketDebuggerUrl"]
        except Exception:
            time.sleep(.2)
    raise RuntimeError("Chrome DevTools endpoint did not start")

ws = websocket.create_connection(endpoint(), timeout=10)
counter = 0
def call(method, params=None):
    global counter
    counter += 1; ident = counter
    ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
    while True:
        msg = json.loads(ws.recv())
        if msg.get("id") == ident: return msg.get("result", {})

try:
    call("Page.enable"); call("Runtime.enable"); time.sleep(1)
    sizes = [(1920,1080),(1280,720),(768,1024),(375,667),(667,375)]
    report = []
    for width, height in sizes:
        call("Emulation.setDeviceMetricsOverride", {"width":width,"height":height,"deviceScaleFactor":1,"mobile":width<700})
        time.sleep(.25)
        expression = """JSON.stringify({slides:[...document.querySelectorAll('.slide')].length,overflow:[...document.querySelectorAll('.slide')].map((s,i)=>({i:i+1,vertical:s.scrollHeight>s.clientHeight+1,horizontal:s.scrollWidth>s.clientWidth+1,sh:s.scrollHeight,ch:s.clientHeight})).filter(x=>x.vertical||x.horizontal),controls:!!document.querySelector('#next'),progress:!!document.querySelector('#progress')})"""
        value = call("Runtime.evaluate", {"expression":expression,"returnByValue":True})["result"]["value"]
        report.append({"viewport":f"{width}x{height}", **json.loads(value)})
    call("Emulation.setDeviceMetricsOverride", {"width":1280,"height":720,"deviceScaleFactor":1,"mobile":False})
    for slide_number in (1, 4, 8, 10, 14, 17):
        call("Runtime.evaluate", {"expression":f"document.querySelectorAll('.slide')[{slide_number-1}].scrollIntoView({{behavior:'auto'}})"})
        time.sleep(1.1)
        shot = call("Page.captureScreenshot", {"format":"png","captureBeyondViewport":False})
        Path(f"deliverables/presentation-preview-{slide_number:02}.png").write_bytes(base64.b64decode(shot["data"]))
    print(json.dumps(report, indent=2))
    if any(item["overflow"] or item["slides"] != 17 or not item["controls"] for item in report): raise SystemExit(1)
finally:
    ws.close(); proc.terminate(); proc.wait(timeout=10)
