# cloe-care

Système de gestion d'incidents pour la plateforme Cloe. Indépendant de cloe-api, cloe-flow et cloe-gui — reste accessible même quand le produit principal est en panne.

## Stack

- FastAPI + Uvicorn (Python 3.11)
- SQLite avec WAL (`care.db`)
- Docker isolé sur `cloe-internal` + `traefik-public` + `socket-proxy-readonly` (LOGS only)
- Traefik routing `care.hellocloe.fr`
- Port `127.0.0.1:8900`

## Principes

- **Read-only par défaut** sur les volumes clients. Toute écriture passe par `cloe-api /internal/apply-patch`.
- **Single-pass LLM** : élicitation Haiku, investigation Sonnet 1 passe + vision en stage 2 paresseux.
- **Coût opérateur** : `X-Operator-Bill: true` sur tous les appels `cloe-proxy`.
- **Zéro jargon technique** dans les messages publics. Tout passe par `notification.public_message`.

## Développement local

```bash
cp .env.example .env
# Remplir les secrets (au minimum JWT_SECRET, identique à cloe-api)
docker compose up --build
curl http://127.0.0.1:8900/health
```

## Documentation

Voir `cloe-care-specs/` dans le repo `cloe` pour le plan d'implémentation complet.
