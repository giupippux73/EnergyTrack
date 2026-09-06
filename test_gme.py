import requests, re
headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64)'}

# Test endpoint GME
url = 'https://gme.mercatoelettrico.org'
r = requests.get(url, headers=headers, timeout=10, verify=False)
print('GME home:', r.status_code)

# Cerca link a file download nella pagina
links = re.findall(r'href=["\']([^"\']*(?:zip|csv|xml|xls)[^"\']*)["\']', r.text, re.IGNORECASE)
for lnk in links[:10]:
    print('  Link trovato:', lnk[:100])

# Prova endpoint diretto statistiche mensili PUN
# Il GME pubblica i prezzi medi mensili su una pagina pubblica
url2 = 'https://gme.mercatoelettrico.org/GME/WebServices/Public/GMEPubblica.asmx/GetPrezziMensiliMGP'
r2 = requests.get(url2, headers=headers, timeout=10, verify=False)
print('\nGetPrezziMensiliMGP:', r2.status_code)
print(r2.text[:300])
