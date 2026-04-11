import urllib.request
import json
import sys

def check_template_revisions():
    url = "https://en.wikipedia.org/w/api.php?action=query&prop=revisions&titles=Template:Thomism&rvprop=timestamp|content&rvslots=main&rvlimit=50&rvdir=newer&format=json"
    
    while True:
        req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        try:
            with urllib.request.urlopen(req) as response:
                data = json.loads(response.read().decode())
                
                pages = data['query']['pages']
                page_id = list(pages.keys())[0]
                
                if 'revisions' not in pages[page_id]:
                    print("No revisions found.")
                    break
                    
                for rev in pages[page_id]['revisions']:
                    content = rev.get('slots', {}).get('main', {}).get('*', '')
                    if 'Verheerlijking_van_de_Heilige_Thomas_van_Aquino' in content or 'Thomas_Aquinas' in content and ('.jpg' in content or '.png' in content):
                        print(f"Found image in Template:Thomism revision at: {rev['timestamp']}")
                        return
                
                if 'continue' in data:
                    rvcontinue = data['continue']['rvcontinue']
                    url = f"https://en.wikipedia.org/w/api.php?action=query&prop=revisions&titles=Template:Thomism&rvprop=timestamp|content&rvslots=main&rvlimit=50&rvdir=newer&format=json&rvcontinue={rvcontinue}"
                else:
                    print("Finished checking all revisions. Image not found in Template:Thomism.")
                    break
        except Exception as e:
            print(f"Error: {e}")
            break

if __name__ == "__main__":
    check_template_revisions()
