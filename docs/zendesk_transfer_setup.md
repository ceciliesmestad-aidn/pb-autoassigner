# Productboard → Zendesk: slik skrur du det på

Når noen setter taggen `send-to-zendesk` på et notat i Productboard, lager AutoAssigner en supportsak i Zendesk. Innsenderen får vanlig «vi har mottatt saken din»-e-post. Notatet får taggen `sent-to-zendesk`, blir tildelt Torstein og markert som prosessert.

Funksjonen er **av** til du skrur den på i steg 5.

## 1. Få tilgang fra Knut Ole

Send meldingen fra chatten, og vent på svar på punktene der. Uten API-token kan du bare gjøre steg 2.

## 2. Prøvekjøring uten Zendesk (kan gjøres nå)

1. Legg taggen `send-to-zendesk` på testforslaget ditt i Productboard.
2. Åpne Terminal i mappen PB_assignerV2, og kjør én kommando om gangen:

```bash
source .venv/bin/activate
```

```bash
python -m backend.cli zendesk-transfer --dry-run
```

Dette **leser bare**. Ingen sak lages, og PB endres ikke. Se etter:

- `"found": 1`: notatet ditt ble funnet
- `"owner_verified": true`: Torstein finnes i PB
- `"outcome": "would_transfer"` og en skjult e-post (`k***@...`): innsenderen ble funnet
- `"needs_review"`: notatet mangler innsender med e-post

## 3. Legg inn Zendesk-nøkler

**På Macen:** åpne `config.toml` og fyll inn `email` og `api_token` under `[zendesk]`. Filen blir aldri med i git.

**I GitHub:** repo → Settings → Secrets and variables → Actions → New repository secret:

- `ZENDESK_EMAIL`
- `ZENDESK_API_TOKEN`

Fyll inn `group_id` hvis Knut Ole gir deg en.

## 4. Kjør prøvekjøringen på nytt

Nå sjekker den også Zendesk om saken allerede finnes. Svaret skal fortsatt være `would_transfer`.

## 5. Én ekte overføring (testforslaget ditt)

1. Sett `enabled = true` under `[zendesk]` i `config.toml`.
2. Kjør:

```bash
python -m backend.cli zendesk-transfer
```

3. Sjekk i Zendesk: emne, tekst, innsender, taggen `from_productboard` og et internt notat med PB-lenken.
4. Sjekk i PB: taggen `sent-to-zendesk`, eier Torstein, prosessert, og `send-to-zendesk` er borte.
5. Lukk testsaken i Zendesk.

## 6. Skru det på i den daglige skykjøringen

Sett `enabled = true` under `[zendesk]` i `config.ci.toml`, commit og push. Etter det tar den daglige kjøringen dette automatisk, og Slack-oppsummeringen viser tallene.

## Hvis noe går galt

- **Feil i Zendesk:** taggen blir stående, og den neste kjøringen prøver igjen. Det lages aldri to saker for samme notat.
- **«blocked» i Slack:** les grunnen. Vanligst er manglende nøkler eller at Torstein ikke finnes som medlem i PB.
- **Skru av:** sett `enabled = false` igjen.
