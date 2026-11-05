# SNCF Open Data pour Home Assistant

Suivez vos trains SNCF (TER, Intercités, TGV) dans Home Assistant avec les
**données ouvertes officielles** : horaires, retards, **suppressions explicites**
et messages de perturbation. **Aucun compte ni clé API** n'est nécessaire.

- Plusieurs gares d'arrivée par trajet (« Rabastens **ou** Saint-Sulpice »).
- Les trains supprimés restent affichés comme supprimés, y compris ceux que la
  SNCF a déjà retirés des horaires publiés (grèves, travaux).
- Une entité événement pour vos automatisations : retard, retard qui change,
  retour à l'heure, suppression, voie affichée, changement de voie.
- Cars de substitution en option, par trajet : les jours de grève ou de
  travaux, ce sont souvent eux qui circulent.
- Voies en option, depuis les tableaux de départs Gares & Connexions (source
  non officielle, voir plus bas).
- Téléchargements limités : le temps réel n'est consulté qu'autour des plages
  horaires de vos trajets.

## Installation

Home Assistant **2026.10** ou plus récent.

1. Dans **HACS**, menu ⋮, **Dépôts personnalisés** : ajoutez
   `https://github.com/enavarro222/ha-sncf-opendata`, catégorie **Intégration**.
2. Installez **SNCF Open Data**, puis redémarrez Home Assistant.
3. **Paramètres → Appareils et services → Ajouter une intégration →
   SNCF Open Data**, puis validez (le flux SNCF est testé).

Sans HACS : copiez `custom_components/sncf_opendata` dans le dossier
`custom_components` de votre configuration, puis redémarrez.

Au premier démarrage, l'intégration télécharge les horaires nationaux (environ
6 Mo) et garde en cache la partie utile à vos trajets.

## Ajouter un trajet

Sur la page de l'intégration, **Ajouter un trajet** :

1. tapez le nom de la gare de départ et des gares d'arrivée (plusieurs noms
   séparés par des virgules ; « St » et « Ste » sont compris) ;
2. choisissez les gares dans les résultats ;
3. donnez un nom, une plage horaire de départ (par exemple 16:30 – 20:00),
   les jours, le seuil de retard, le nombre de trains listés, et s'il faut
   inclure les autocars.

Seuls les **trajets directs** sont suivis : ceux où l'on peut monter à la gare
de départ et descendre à l'une des gares d'arrivée. Le nom, la plage, les
jours, les seuils et les autocars se modifient ensuite ; pour d'autres gares,
ajoutez un trajet.

### Autocars

Par défaut, un trajet ne suit que les trains. Avec **Inclure les autocars**,
il suit aussi les services routiers de la SNCF : cars de substitution pendant
une grève ou des travaux, et cars TER réguliers. Ils apparaissent comme les
trains, avec leurs retards et suppressions quand le temps réel en donne, et un
`mode` qui vaut `Car TER` (ou `Car` pour un car connu seulement du temps
réel). Leur numéro est celui de la SNCF (par exemple `462034`).

Chaque trajet est un appareil avec ses entités.

### Entités d'un trajet

| Entité | Description |
|---|---|
| `sensor.<trajet>_prochain_depart` | Heure de départ prévue (retard compris) du prochain train qui circule. L'attribut `trains` liste les trains de la plage, supprimés compris. |
| `binary_sensor.<trajet>_perturbation` | Actif si un train à venir de la plage est supprimé ou en retard au-delà du seuil. |
| `event.<trajet>_changement_de_train` | Se déclenche à chaque changement (voir ci-dessous). |

Le nom exact des entités dépend de la langue de Home Assistant.

Chaque train de l'attribut `trains` (et chaque événement) contient :
`number`, `mode`, `origin`, `destination`, `destinations` (toutes les gares
d'arrivée desservies), `terminus`, `scheduled_departure`,
`expected_departure`, `scheduled_arrival`, `expected_arrival`, `delay`
(minutes), `status` (`on_time`, `delayed`, `cancelled` ou `scheduled` quand
aucune information temps réel n'est disponible), `realtime`, `added`,
`cancel_reason`, `platform` et `alerts` (titres des messages de perturbation).

### Entité de l'intégration

`binary_sensor.sncf_open_data_flux_temps_reel` (diagnostic) indique si le flux
temps réel répond avec des données récentes.

## Événements

L'entité événement d'un trajet se déclenche avec ces types :

| Type | Quand |
|---|---|
| `delayed` | Le retard atteint le seuil. |
| `delay_changed` | Le retard a varié d'au moins le seuil depuis la dernière annonce (un retard qui grimpe d'une minute à la fois finit par être annoncé). |
| `back_on_time` | Le retard repasse sous le seuil. |
| `cancelled` | Le train est supprimé, ou ne dessert plus la gare de départ ou aucune des gares d'arrivée. |
| `platform_assigned` | La voie est affichée. |
| `platform_changed` | La voie change. |

Les données de l'événement sont celles du train, plus `route`,
`previous_delay` et `previous_platform`. Une panne du flux temps réel ne
déclenche jamais de faux « retour à l'heure », et un redémarrage de Home
Assistant ne répète pas les perturbations déjà connues.

### Exemple : prévenir sur son téléphone

```yaml
triggers:
  - trigger: state
    entity_id: event.retour_changement_de_train
    not_from: unavailable
actions:
  - variables:
      e: "{{ trigger.to_state.attributes }}"
  - action: notify.mobile_app_mon_telephone
    data:
      title: "🚆 {{ e.route }}"
      message: >-
        {% set h = as_datetime(e.scheduled_departure).strftime('%H:%M') %}
        {% if e.event_type == 'cancelled' %}❌ Le {{ h }} est supprimé.
        {% elif e.event_type in ['delayed', 'delay_changed'] %}⚠️ Le {{ h }} a {{ e.delay }} min de retard.
        {% elif e.event_type == 'back_on_time' %}✅ Le {{ h }} est de nouveau à l'heure.
        {% else %}🔀 Le {{ h }} part voie {{ e.platform }}.{% endif %}
        {% if e.alerts %}{{ e.alerts | join(' · ') }}{% endif %}
```

## Options

| Option | Par défaut | |
|---|---|---|
| Intervalle de mise à jour temps réel | 120 s | Pendant les plages des trajets (d'une heure avant jusqu'à 30 min après). Chaque mise à jour télécharge environ 450 ko. En dehors, rien n'est téléchargé. |
| Afficher les voies | non | Voir ci-dessous. |

### Voies (source non officielle)

Les voies ne font pas partie des données ouvertes de la SNCF. Avec cette
option, l'intégration les lit sur les tableaux de départs du site Gares &
Connexions, par une adresse **non documentée** qui peut changer ou disparaître
à tout moment. L'intégration est faite pour que cela ne casse rien : réponses
validées, délai court, nouvelles tentatives espacées après une panne, voies
oubliées au bout de 15 minutes plutôt que montrées périmées, un seul message
dans le journal quand la source tombe et quand elle revient. Les voies sont en
général affichées peu avant le départ.

## Limites connues

- La SNCF ne garantit pas une couverture temps réel complète des TER. Un train
  sans information reste en `scheduled` (horaire prévu), jamais présenté comme
  « à l'heure » par défaut.
- Les trains déjà passés disparaissent du flux temps réel.
- Trajets directs uniquement, pas de correspondances.
- Les voies ne concernent que les trains.
- Une plage horaire ne peut pas passer minuit.
- Les flux temps réel sont ignorés s'ils datent de plus de 10 minutes.

## Données

Les données viennent de SNCF Voyageurs, jeu « Réseau SNCF TGV, Intercités et
TER » du Point d'accès national
[transport.data.gouv.fr](https://transport.data.gouv.fr/datasets/horaires-sncf),
sous licence [ODbL](https://opendatacommons.org/licenses/odbl/1-0/) :
horaires théoriques (GTFS), mises à jour des trajets et messages de
perturbation (GTFS-RT). Ce projet est indépendant de la SNCF.

## Développement

Python 3.14 et [uv](https://docs.astral.sh/uv/) :

```sh
make venv    # environnement avec Home Assistant et les outils de test
make check   # ruff, mypy strict, tests avec couverture
```

`tests/unit` teste le cœur sans Home Assistant (horaires, temps réel,
fusion, détection des changements, voies), dont des tests de non-régression sur
des extraits des vrais flux du 4 novembre 2026 (`tests/fixtures/real`) et du
5 novembre 2026, jour de grève avec cars de substitution
(`tests/fixtures/real_2026_11_05`).
`tests/ha` teste l'intégration dans Home Assistant avec des serveurs simulés.

## Licence

MIT, voir [LICENSE](LICENSE).
