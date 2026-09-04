# INFRAWATCH — Point de situation national V1

## Principe d'architecture

Cette brique est volontairement séparée du moteur de scoring.

1. Les fichiers InfraWatch (`dashboard.json`, `latest_live.json`, etc.) restent l'autorité factuelle.
2. `generate_point_national.py` construit un `fact_packet` déterministe.
3. Le LLM reçoit ce paquet et produit uniquement de la prose analytique.
4. Le schéma de sortie du LLM ne contient aucun champ de niveau N0-N4.
5. Un garde-fou rejette toute réponse LLM contenant un code `N0` à `N4`.
6. Les niveaux officiels et les métriques sont injectés ensuite par le générateur.
7. Le générateur produit :
   - `points/latest.json`
   - `points/latest.html`
   - `points/latest.pdf`
   - les archives datées sous `points/archive/`
   - `points/manifest.json`

## Fichiers à copier dans le dépôt public

- `generate_point_national_v1.py` -> `generate_point_national.py`
- `point_national_prompt_v1.md` -> `point_national_prompt.md`
- `point_national_analysis_schema_v1.json`
- `generate_point_national.yml` -> `.github/workflows/generate_point_national.yml`

## Secret GitHub requis

`OPENAI_API_KEY`

## Variable GitHub facultative

`POINT_NATIONAL_MODEL`

Valeur conseillée par défaut : `gpt-5.6-terra`.

## Test sans appel LLM

```bash
python generate_point_national.py --source-dir . --output-dir points --schema point_national_analysis_schema_v1.json --cycle 08H00 --facts-only
```

Ce test doit produire `points/fact_packet_latest.json`.

## Test complet manuel

```bash
export OPENAI_API_KEY="..."
python generate_point_national.py --source-dir . --output-dir points --schema point_national_analysis_schema_v1.json --cycle 08H00
```

## Doctrine

Le LLM :
- ne score pas ;
- ne modifie pas les niveaux ;
- ne crée pas de causalité ;
- ne substitue pas une source de secours à InfraWatch lorsque les fichiers InfraWatch sont exploitables ;
- n'interprète pas une absence d'événement comme une preuve de normalité.

Le PDF et l'HTML sont des restitutions analytiques. Ils n'ont aucune autorité sur le moteur.
