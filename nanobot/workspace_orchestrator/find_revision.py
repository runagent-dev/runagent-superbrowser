import requests
import json

S = requests.Session()
S.headers.update({"User-Agent": "MyBot/1.0 (contact@example.com)"})
URL = "https://en.wikipedia.org/w/api.php"

PARAMS = {
    "action": "query",
    "prop": "revisions",
    "titles": "Principle of double effect",
    "rvprop": "timestamp|content",
    "rvslots": "main",
    "rvlimit": "50",
    "rvdir": "newer",
    "format": "json"
}

found = False
while True:
    R = S.get(url=URL, params=PARAMS)
    print(R.status_code)
    print(R.text[:200])
    DATA = R.json()
    pages = DATA["query"]["pages"]
    page_id = list(pages.keys())[0]
    revisions = pages[page_id].get("revisions", [])
    
    for rev in revisions:
        content = rev.get("slots", {}).get("main", {}).get("*", "")
        if ("File:" in content or "Image:" in content) and "Aquinas" in content:
            print(f"Found potential match at {rev['timestamp']}")
            idx = content.find("Aquinas")
            print(content[max(0, idx-100):idx+100])
            found = True
            break
            
    if found:
        break
        
    if "continue" in DATA:
        PARAMS.update(DATA["continue"])
    else:
        print("Not found")
        break
