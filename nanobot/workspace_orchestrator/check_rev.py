import urllib.request
import json

def get_revision_content(timestamp):
    url = f"https://en.wikipedia.org/w/api.php?action=query&prop=revisions&titles=Principle_of_double_effect&rvprop=timestamp|content&rvslots=main&rvlimit=1&rvstart={timestamp}&rvdir=newer&format=json"
    req = urllib.request.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
    try:
        with urllib.request.urlopen(req) as response:
            data = json.loads(response.read().decode())
            pages = data['query']['pages']
            page_id = list(pages.keys())[0]
            if 'revisions' in pages[page_id]:
                return pages[page_id]['revisions'][0].get('slots', {}).get('main', {}).get('*', '')
    except Exception as e:
        print(f"Error: {e}")
    return None

content = get_revision_content("2009-02-19T06:41:05Z")
if content:
    print("Content of revision 2009-02-19T06:41:05Z:")
    for line in content.split('\\n'):
        if '{{' in line:
            print(line)
