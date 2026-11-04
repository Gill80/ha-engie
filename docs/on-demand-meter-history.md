# Historique ENGIE par compteur — fonction à la demande

## Commandes natives

Sur chaque appareil ENGIE Gaz ou Électricité :

- **Période à récupérer** : 90 jours, 12 mois, ou tout l'historique disponible.
- **Prévisualiser l'historique** : lire les API ENGIE et compter les jours, sans écriture.
- **Récupérer l'historique** : importer les statistiques propres au compteur, après vérification.

Les commandes démarrent une tâche en arrière-plan. Une notification Home Assistant
annonce le résultat et l'action engie.meter_history_status donne la progression.

Chaque équipement électrique ou gaz possède également le capteur de diagnostic
**État de récupération de l'historique** : *Aucune tâche*, *En cours*,
*Terminée* ou *Échec*. Ses attributs indiquent les fenêtres traitées, le
pourcentage de progression, la période demandée et, en cas de problème, le
type d'erreur et le code HTTP (sans afficher les données privées du compte).
Les états des tâches sont conservés en mémoire et repartent sur *Aucune tâche*
après un redémarrage de Home Assistant.

Une seule tâche ENGIE est autorisée simultanément par configuration, pour
éviter que plusieurs requêtes ne se disputent la même session et les cookies
de sélection de compte. Si une tâche est déjà en cours, consultez le capteur
d'état correspondant avant de relancer un import.

Les actions avancées sont engie.preview_meter_history, engie.import_meter_history
et engie.meter_history_status. Elles acceptent un meter_entity qui appartient
à un appareil ENGIE, ou un meter_source_id technique. Sans filtre, elles
ciblent tous les compteurs de la seule configuration ENGIE active.

La période peut être 90_days, 365_days, all_available ou custom. En mode
custom, start_date et end_date sont des dates YYYY-MM-DD inclusives, antérieures
au jour courant. Le paramètre include_costs permet de conserver ou d'exclure
les coûts TTC.

## Provenance et séparation des statistiques

Pour une source identifiée par un identifiant de type meter_... :

- engie:meter_..._energy : énergie quotidienne en kWh
- engie:meter_..._cost : coûts quotidiens TTC en EUR quand ENGIE les fournit

Ces statistiques Recorder externes ont source=engie. Les identifiants ne
contiennent ni PCE ni PDL complet et ne peuvent pas désigner une entité LiXee,
Linky, ZiPulses ou un autre compteur physique.

Le tableau de bord Énergie existant n'est PAS modifié par la fonction.
Les statistiques sont propres au compteur de l'intégration ENGIE et ne
sont pas les historiques d'états du capteur conso_quotidienne. Les cartes
natives statistics-graph acceptent directement ces identifiants statistiques.

## Fiabilité

- Import explicitement déclenché seulement (jamais au rafraîchissement normal).
- Fenêtres de 90 jours maximum, basées sur minuit Europe/Paris et adaptées à l'heure d'été.
- Un appel par fenêtre et par compteur, sur son compte contrat ENGIE propre.
- Jusqu'à environ dix ans explorés en mode all_available ; il s'agit d'une
  limite de recherche et non d'une garantie de disponibilité chez ENGIE.
- Les jours courants/partiels et les réponses sans données sont exclus.
- Cache privé dans le Store Home Assistant, indexé par source hashée et jour.
- Import répété sans doublon ; si ENGIE corrige une journée passée, recomputation
  du cumul de ce compteur pour tous les jours ultérieurs.
- Aucun écrit dans Recorder avant la fin de la collecte.
- La prévisualisation ne change aucune statistique et ne modifie pas le Store.
- Les insertions Recorder sont mises en file d'attente et peuvent être
  visibles avec un léger délai.
- Aucune modification des index physiques ni des paramètres du tableau Énergie.

Le service historique obsolète engie.import_history est bloqué si les
anciens capteurs contractuels ont été désactivés.

Les consommations journalières ENGIE peuvent être estimées. Les coûts TTC
ne correspondent pas nécessairement à l'intégralité de la facture mensuelle.
