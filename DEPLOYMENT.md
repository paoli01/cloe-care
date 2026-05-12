# Déploiement cloe-care

Ce document est le runbook minimal pour déployer (et roller back) `cloe-care`
sur le VPS Hostinger en production.

## Pré-requis

- Accès SSH au VPS qui héberge déjà `cloe-api`, `cloe-gui`, `cloe-proxy`
- Accès Cloudflare pour le DNS `hellocloe.fr`
- `SERVICE_SECRET` et `JWT_SECRET` déjà déployés sur `cloe-api`
- Réseaux Docker `cloe-internal` et `traefik-public` existants

## 1. DNS Cloudflare

Sur le dashboard Cloudflare → DNS → `hellocloe.fr` :

| Type | Name | Content       | Proxy | TTL  |
|------|------|---------------|-------|------|
| A    | care | `<VPS_IPV4>`  | Proxied (orange) | Auto |
| AAAA | care | `<VPS_IPV6>` (si dispo) | Proxied | Auto |

SSL/TLS mode → `Full (Strict)`. Vérifier :

```bash
dig +short care.hellocloe.fr
# doit renvoyer une IP Cloudflare (104.x.x.x ou 172.x.x.x)
```

## 2. Cookie cross-subdomain

Pour que `cloe_jwt` soit lisible côté `care.hellocloe.fr`, ajouter
côté `cloe-api/.env` :

```ini
COOKIE_DOMAIN=.hellocloe.fr
```

Puis :

```bash
cd /opt/cloe-api
git pull origin main
docker compose up -d --build cloe-api
docker logs cloe-api --tail 30
```

Les nouveaux logins poseront un cookie valable sur toute la sous-arbo
`.hellocloe.fr`.

## 3. Variables d'environnement cloe-care

Sur le VPS :

```bash
cd /opt/cloe-care
cp .env.example .env
chmod 600 .env
nano .env
```

Renseigner au minimum :

```ini
# Doivent être strictement identiques à cloe-api
JWT_SECRET=<copie depuis /opt/cloe-api/.env>
SERVICE_SECRET=<copie depuis /opt/cloe-api/.env>

# Endpoint apply-patch
CLOE_API_URL=http://cloe-api:8700
CLOE_API_KEY=<même valeur que SERVICE_SECRET>

# LLM via proxy interne
CLOE_PROXY_URL=http://cloe-proxy:8000
OPERATOR_OPENROUTER_KEY=<clé opérateur dédiée pour care>

# Notifications
RESEND_API_KEY=<clé Resend prod>
EMAIL_FROM=cloe@hellocloe.fr
EMAIL_REPLY_TO=care@hellocloe.fr

# Issues GitHub
GITHUB_TOKEN=<token avec scope issues:write sur paoli01/*>
GITHUB_REPO_OWNER=paoli01

# Admins (séparés par virgule, comparés en lowercase)
ADMIN_EMAILS=paul@hellocloe.fr

# Garde-fou — désactive file_replace tant que CARE_SAFE_MODE=true
CARE_SAFE_MODE=true

# Cookies httpOnly secure en HTTPS
COOKIE_SECURE=true
```

## 4. Build et démarrage

```bash
cd /opt/cloe-care
git pull origin main
docker compose build
docker compose up -d
docker compose logs -f cloe-care
```

Vérifier que les tables sont créées :

```bash
docker exec cloe-care sqlite3 /data/care/care.db ".tables"
# tickets chat_messages ticket_events notifications attachments
# apply_patch_audit global_fix_proposals known_incidents
# pattern_fingerprints admin_decisions
```

## 5. Validation Traefik

```bash
curl -I https://care.hellocloe.fr/health
# HTTP/2 200
curl https://care.hellocloe.fr/health
# {"status":"ok","service":"cloe-care","version":"0.1.0","queue_size":0,"worker_alive":true}
```

Si erreur 502 : Traefik ne voit pas le container. Vérifier :

```bash
docker inspect cloe-care --format '{{json .NetworkSettings.Networks}}' | jq
# doit contenir "traefik-public"
```

Si absent :

```bash
docker network connect traefik-public cloe-care
docker compose restart cloe-care
```

## 6. Smoke test end-to-end

```bash
./scripts/smoke_test.sh https://care.hellocloe.fr
```

Le script :
1. Génère un JWT de test via `cloe-api`
2. Vérifie `/health`
3. Crée un ticket, envoie un message, soumet
4. Polle le statut pendant 120 s jusqu'à un état terminal

## 7. Monitoring

Healthcheck Uptime Kuma (ou équivalent) :

```yaml
url: https://care.hellocloe.fr/health
interval: 60s
notification_on: down
```

À surveiller pendant 48 h :
- Tickets/jour
- Taux `rejected_review` (anti-abus)
- Taux `escalated` (échec auto-fix)
- ACU consommé par jour : `sqlite3 /opt/cloe/proxy/cloe_proxy.db "SELECT SUM(cost_eur) FROM cost_events WHERE client_id='operator' AND created_at > date('now','-1 day')"`
- Latence p50/p95 du worker (cf. logs `cloe-care.worker`)

## 8. Rollback

### Rollback `cloe-care` seul

```bash
cd /opt/cloe-care
git log --oneline -10
git checkout <COMMIT_PRECEDENT>
docker compose up -d --build cloe-care
```

### Rollback `cloe-api /internal/apply-patch`

```bash
cd /opt/cloe-api
git revert <COMMIT_INTERNAL_APPLY>
docker compose up -d --build cloe-api
```

### Désactivation rapide via flag

Pour désactiver l'entrée Support dans le GUI :

```bash
cd /opt/cloe-gui
# Ajouter dans .env.local :
echo "NEXT_PUBLIC_SUPPORT_ENABLED=false" >> .env.local
docker compose up -d --build cloe-gui
```

Et conditionner l'affichage des liens nav dans `LegacyDashboard.tsx` sur
ce flag.

### Restauration d'un fichier client corrompu

Les backups sont dans `/opt/cloe/care_backups/{client_id}/{ticket_id}/*.bak` :

```bash
ls -la /opt/cloe/care_backups/<client_id>/
cp /opt/cloe/care_backups/<client_id>/<ticket_id>/config.yaml.<timestamp>.bak \
    /opt/cloe/clients/<client_id>/config.yaml
docker restart hermes_<client_id>
```

## 9. Garde-fous prod initiaux

Pendant les 7 premiers jours :

- `CARE_SAFE_MODE=true` (bloque `file_replace`, n'autorise que
  `session_delete` et `container_restart`)
- `ADMIN_EMAILS` rempli (étape 11 _admin view_ → revue humaine de
  chaque fix `config_client`/`data_client`)
- Surveiller les `rejected_review` pour ajuster l'anti-abus
- Surveiller les `escalated` avec cause `safe_mode_file_replace_disabled`
  pour évaluer quand basculer en mode normal

Quand le quotidien est stable :

```bash
sed -i 's/^CARE_SAFE_MODE=true$/CARE_SAFE_MODE=false/' .env
docker compose up -d cloe-care
```

## 10. Évolutions identifiées (hors MVP)

Voir `cloe-care-specs/08_DEPLOYMENT.md` § _Évolutions identifiées_.
