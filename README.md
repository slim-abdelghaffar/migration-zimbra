# Migration PST → Zimbra

Petite plateforme web (FastAPI + SQLite + java-libpst) pour importer des PST dans des comptes Zimbra 10.1
existants, sans mot de passe utilisateur : admin Zimbra + jetons délégués
(AuthRequest → DelegateAuthRequest → GetInfoRequest).

## Installation

### Poste de développement (WSL)

```bash
./install.sh && ./run.sh        # http://localhost:8000, sans mot de passe d'accès (local uniquement)
```

### VM cliente (Ubuntu 22.04 / 24.04)

**Avec Internet** (dépôt Git) :

```bash
git clone <url-du-depot> nextstep-migration && cd nextstep-migration
./install.sh --reseau           # dépendances, HTTPS auto-signé, noms/IP autorisés, mot de passe d'accès
./install-service.sh            # service systemd : démarrage automatique
```

**Sans Internet** : sur le poste de développement, `./package.sh` produit
`dist/nextstep-migration-<version>.tar.gz` (code, java-libpst et dépendances Python 3.10 → 3.14).
Le copier sur la VM, puis :

```bash
tar xzf nextstep-migration-1.0.tar.gz && cd nextstep-migration-1.0
./install.sh --reseau && ./install-service.sh
```

Seuls Java (`openjdk-21-jdk-headless`) et `python3-venv` viennent des paquets Ubuntu (dépôt local ou miroir du client).

Ouvrir ensuite `https://<nom-ou-ip-de-la-vm>:8443` depuis le navigateur (certificat auto-signé à accepter,
ou remplacer `TLS_CERT` / `TLS_KEY` dans `migration.env` par un certificat du client).
Changer le mot de passe d'accès : `./set-password.sh`. Journaux du service : `journalctl -u nextstep-migration -f`.

### Dossier PST partagé avec la VM Windows

Recommandé : la VM Windows partage un dossier (ex. `\\WIN-EXPORT\PST$`), la VM Ubuntu le monte **en lecture seule** :

```bash
sudo apt install cifs-utils && sudo mkdir -p /mnt/pst
echo 'username=svc_pst
password=********
domain=CLIENT' | sudo tee /root/.smb-pst >/dev/null && sudo chmod 600 /root/.smb-pst
echo '//WIN-EXPORT/PST$ /mnt/pst cifs ro,credentials=/root/.smb-pst,uid=1000,iocharset=utf8,_netdev,nofail 0 0' | sudo tee -a /etc/fstab
sudo mount /mnt/pst
```

Dans **Configuration → Dossier PST → Parcourir…**, le volume monté apparaît en tête (« partagé · CIFS »),
avec le nombre de PST qu'il contient. Exporter les PST localement sous Windows puis les copier dans le partage :
la plateforme attend la fin de chaque copie avant de l'analyser.

### Mise à jour

```bash
git pull && ./install.sh && sudo systemctl restart nextstep-migration      # données et réglages conservés
```

## Écrans

1. **Connectivité** : DNS, TCP 7071 (admin), TCP 443 (utilisateur), certificat TLS, AuthRequest admin.
2. **Configuration** : URLs, compte et mot de passe admin (en mémoire uniquement, perdu au redémarrage),
   dossier PST, parallélisme (1 à 5), fuseau horaire par défaut.
3. **Inventaire** : `<email>.pst` → compte vérifié par GetAccountRequest. Statuts : prêt / compte introuvable /
   nom invalide (y compris un alias : renommer le PST au nom principal). Seuls les comptes prêts sont migrables.
4. **Test jeton** : DelegateAuthRequest + GetInfoRequest, lecture seule.
5. **Migration** : simulation par défaut (aucune écriture) ; import réel après confirmation dans la page.
   Mode delta optionnel : mails depuis une date.
6. **Suivi** : progression et compteurs par compte et par type, journal par compte, arrêt / reprise, rapport CSV.

## Déroulement par compte

1. Jeton délégué neuf, GetInfoRequest. **Seul blocage** : nom du PST ≠ compte renvoyé → « bloqué ».
2. Extraction java-libpst (lecture seule) dans `work/run<N>-<email>/`, supprimé à la fin.
3. Détection automatique de ce que contient le PST : mails, calendrier, contacts, tâches.
   Un type absent est indiqué « absent », ce n'est pas une erreur.
4. Import : arborescence recréée (CreateFolderRequest), mails par `/service/upload` + AddMsgRequest
   (date d'origine, lu/non lu, drapeau, répondu/transféré), calendrier en `.ics` (récurrences, exceptions,
   participants, fuseaux), tâches en `.ics` (VTODO), contacts en `.vcf`, par lots de 50.
5. Un élément illisible ou non convertible est compté en erreur et journalisé ; le compte continue.

Idempotence : chaque élément importé est enregistré (Message-ID pour les mails) dans `data/state.db` ;
une relance ou une reprise n'importe que ce qui manque.

Non migrés (signalés dans le rapport) : notes, journal, listes de distribution, dossiers techniques Outlook
(Problèmes de synchronisation, Boîte d'envoi, dossiers de recherche…).

## État réel des boîtes

- Tableau de bord : taille de chaque boîte (GetQuotaUsageRequest, admin), rafraîchie à chaque visite.
- Page d'un compte (clic sur un compte) : contenu réel de la boîte, dossier par dossier, lu en direct
  (GetFolderRequest avec un jeton délégué), comparé à ce que la plateforme a importé.
- Avant chaque import, la plateforme relit les Message-ID / UID présents dans la boîte (SearchRequest) et
  oublie ce qui a été supprimé dans Zimbra : une relance réimporte uniquement ce qui manque.
  Si le serveur ne renvoie pas ces identifiants, l'historique local est conservé (aucun doublon).
- « Vérifier les boîtes migrées » fait ce contrôle pour tous les comptes ; « Réinitialiser l'historique »
  (page du compte) repart de zéro.

## Sécurité

- Mot de passe admin et jetons : jamais sur disque, jamais dans les logs, jamais affichés.
- Écriture uniquement dans les comptes « prêts » de l'inventaire, après concordance.
- Vérification TLS toujours active (magasin système ou CA fournie).
- `data/state.db` : paramètres non secrets, inventaire, lots, éléments importés.
  `logs/app.log` et `logs/runs/<lot>/<email>.log` : journaux.

## Identité visuelle

Nom, couleurs et logo dans `app/brand.py`. Pour utiliser le logo officiel NextStep, déposer
`app/static/brand/logo.svg` (ou `logo.png`) : il remplace automatiquement le monogramme par défaut.
La page **Fonctionnement** de l'interface explique le principe de la plateforme (support de présentation).
