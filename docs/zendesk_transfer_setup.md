# Productboard → Zendesk: slik skrur du det på

Når noen setter taggen `send-to-zendesk` på et notat i Productboard, lager AutoAssigner en supportsak i Zendesk. Innsenderen får vanlig «vi har mottatt saken din»-e-post. Notatet får taggen `sent-to-zendesk`, blir tildelt Torstein og markert som prosessert.

Funksjonen er **av** i den daglige kjøringen til du skrur den på i steg 4.

## 1. Tilgang (ferdig 2026-11-05)

Knut Ole har lagt inn en OAuth-klient som repo-secrets i GitHub:

- `ZENDESK_OPERATOR_OAUTH_CLIENT_ID`
- `ZENDESK_OPERATOR_OAUTH_CLIENT_SECRET`

Klienten bruker «client credentials», og sakene opprettes som operatør-brukeren.

## 2. Prøvekjøring i GitHub

1. Gå til GitHub, åpne repoet `pb-autoassigner` og klikk **Actions**.
2. Velg **Zendesk transfer (manual)** i lista til venstre.
3. Klikk **Run workflow**, velg `mode = dry-run` og klikk den grønne knappen.
4. Åpne kjøringen og steget **Run transfer**.

Se etter:

- `"owner_verified": true`
- `"would_transfer": 1`

Hvis Zendesk-innloggingen feiler, står det `oauth` i feilmeldingen.

## 3. Én ekte overføring (testforslaget ditt)

1. Klikk **Run workflow** igjen.
2. Velg `mode = one-note`.
3. Lim inn notat-ID-en i `note_id`. ID-en til testnotatet er `fdcd4574-0d47-4497-9857-b0a75fa51a85`.
4. Sjekk i Zendesk: emne, tekst, deg som innsender, taggen `from_productboard` og et internt notat med PB-lenken.
5. Sjekk i PB: taggen `sent-to-zendesk`, eier Torstein, prosessert, og `send-to-zendesk` er borte.
6. Lukk testsaken i Zendesk.

Bare notatet med denne ID-en blir sendt. Andre notater med taggen venter.

## 4. Skru det på i den daglige kjøringen

Sett `enabled = true` under `[zendesk]` i `config.ci.toml`, commit og push. Etter det tar den daglige kjøringen dette automatisk, og Slack-oppsummeringen viser tallene.

## Lokalt på Macen (valgfritt)

Hvis du har klient-ID og hemmelighet, kan du legge dem inn i `config.toml` under `[zendesk]`. Da fungerer de samme kommandoene lokalt:

```bash
python -m backend.cli zendesk-transfer --dry-run
```

```bash
python -m backend.cli zendesk-transfer --only <notat-id>
```

## Hvis noe går galt

- **Feil i Zendesk:** taggen blir stående, og den neste kjøringen prøver igjen. Det lages aldri to saker for samme notat.
- **«blocked» i Slack:** les grunnen. Vanligst er manglende nøkler eller at Torstein ikke finnes som medlem i PB.
- **Skru av:** sett `enabled = false` igjen.
