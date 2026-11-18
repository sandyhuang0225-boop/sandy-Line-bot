import asyncio
import websockets
import json
import urllib.request
import subprocess
import time

proc = subprocess.Popen([
    r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
    '--headless',
    '--remote-debugging-port=9238',
    'file:///C:/Users/Sandy%20Huang/OneDrive/Desktop/line_bot/nhi_demo.html'
])
time.sleep(2)

async def check():
    targets = json.loads(urllib.request.urlopen('http://localhost:9238/json').read())
    page = [t for t in targets if t['type'] == 'page'][0]
    async with websockets.connect(page['webSocketDebuggerUrl']) as ws:
        await ws.send(json.dumps({'id': 1, 'method': 'Runtime.enable'}))
        await ws.recv()
        
        test_script = '''
        (() => {
            const query = "病患腫瘤 2 公分、淋巴結 2 顆、Grade 1";
            const norm = normalizeMedicalSpeech(query);
            const res = evaluateClinicalQuery(query);
            return {
                query,
                norm,
                nodes: res.nodes,
                size: res.size,
                grade: res.grade,
                eligible: res.eligible,
                spoken: res.spoken
            };
        })()
        '''
        await ws.send(json.dumps({'id': 2, 'method': 'Runtime.evaluate', 'params': {'expression': test_script, 'returnByValue': True}}))
        while True:
            msg = json.loads(await ws.recv())
            if msg.get('id') == 2:
                with open('nodes2_verify.json', 'w', encoding='utf-8') as f:
                    json.dump(msg.get('result', {}).get('result', {}).get('value', {}), f, ensure_ascii=False, indent=2)
                break

try:
    asyncio.run(check())
finally:
    proc.kill()
