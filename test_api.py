import requests, json, time

# 1. Verifica server e storico Engie
r = requests.get('http://localhost:5000/api/engie')
print('Storico Engie status:', r.status_code)
engie = r.json()
print('Fatture caricate:', len(engie.get('fatture', [])))
tot = sum(f['importo'] for f in engie.get('fatture', []))
print('Totale speso Engie:', tot)
print()

# 2. Avvia aggiornamento prezzi
r2 = requests.post('http://localhost:5000/api/aggiorna')
print('Aggiornamento avviato:', r2.json().get('messaggio'))
print()

# 3. Polling
for i in range(25):
    time.sleep(4)
    r3 = requests.get('http://localhost:5000/api/stato')
    stato = r3.json()
    in_corso = stato.get('in_corso', True)
    print(f'Polling {i+1}: in_corso={in_corso}')
    if not in_corso:
        print('Aggiornato il:', stato.get('aggiornato_il'))
        break

# 4. Leggi i dati
r4 = requests.get('http://localhost:5000/api/dati')
if r4.status_code == 200:
    dati = r4.json()
    if dati.get('errore'):
        print('ERRORE:', dati['errore'])
    else:
        pun_m = dati.get('pun_monthly', {})
        psv_m = dati.get('psv_monthly', {})
        print()
        print('PUN mensili disponibili:', len(pun_m))
        for k, v in list(pun_m.items())[-5:]:
            print(f'  {k}: {v:.5f} euro/kWh')
        print()
        print('PSV mensili disponibili:', len(psv_m))
        for k, v in list(psv_m.items())[-5:]:
            print(f'  {k}: {v:.4f} euro/Smc')
