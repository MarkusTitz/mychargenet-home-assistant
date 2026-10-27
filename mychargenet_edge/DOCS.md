# mychargenet Edge – Home-Assistant-Pilot

Diese Pilot-App liest ausschließlich die ausdrücklich eingetragenen
Home-Assistant-Leistungssensoren und sendet sie ausgehend per HTTPS an
`api.mychargenet.com`. Sie führt keine Home-Assistant-Aktion, keinen
OCPP-Befehl und keine Lastmanagement-Freigabe aus.

Die Zieladresse ist fest an die mychargenet-API gebunden und kann in der
App-Konfiguration nicht auf einen fremden Server umgestellt werden.

## Upgrade auf 0.4.0: Regelfelder und Hilfsfelder

Die Regelfelder `building_power_entity` (Summe), `grid_power_entity` (Netz)
und `phase_l1_entity` bis `phase_l3_entity` müssen auf `sensor.*` zeigen.
Hilfsfelder für PV, Batterie, verfügbare Leistung, Spannung und Strom dürfen
auch andere syntaktisch gültige Entitätsdomänen verwenden, etwa `input_number.*`.

Ist ein Regelfeld anders belegt (z. B. `number.*` oder `input_number.*`), stoppt
der HA-Worker mit `configuration_error`: Es werden keine HA-Messwerte übertragen,
auch keine Hilfswerte. Protokoll und Health zeigen die Ursache und den Korrekturweg.
Passende Leistungssensoren in Home Assistant bereitstellen, die Regelfelder in
der Add-on-Konfiguration auf deren `sensor.*`-IDs ändern, speichern und das Add-on
neu starten. Danach Health und aktuelle Werte im Portal prüfen; Quellenfreigabe
und Automatikfreigabe bleiben eigene Vorgänge.

## Einrichtung

1. In mychargenet beim richtigen Kunden und Standort ein lokales
   Energie-Gateway vom Typ `Home Assistant` anlegen.
2. Den einmalig angezeigten Kopplungscode innerhalb von 15 Minuten in
   `pairing_code` eintragen.
3. Für die Portalregelung die Summe bei `building_power_entity` eintragen;
   der Standort legt fest, ob diese Ladung enthält oder ausschließt.
4. App speichern, starten und im Protokoll sowie im mychargenet-Portal den
   Online-Status prüfen.
5. Nach erfolgreicher Kopplung `pairing_code` wieder leeren, speichern und die
   App neu starten. Die widerrufbare Geräteidentität bleibt geschützt unter
   `/data/mychargenet-edge` erhalten.

Unterstützte Einheiten sind `W`, `kW`, `MW`, `V`, `kV`, `A` und `mA`. Unbekannte, nicht numerische
oder ältere Werte werden nicht als live übertragen. Wenn nur ein Teil der
Sensoren aktuell ist, meldet die App einen eingeschränkten Zustand.

Die Felder `phase_l1_entity` bis `phase_l3_entity` stehen fuer Leistung,
`phase_l1_voltage_entity` bis `phase_l3_voltage_entity` fuer Spannung und
`phase_l1_current_entity` bis `phase_l3_current_entity` fuer Strom. Nicht
vorhandene Phasenfelder duerfen leer bleiben.

Die App sendet Feldqualität mit dem Protokoll `field-meta-v1`.
`ha_state_changed` beschreibt eine HA-Wertänderung (`last_changed`, ersatzweise
`last_updated`), keine physische Messzeit; `last_reported` ist nur ein
Lebenszeichen und verjüngt einen konstanten Wert nicht.
Die App kann ältere Phasen bis zu 30 Minuten als Diagnose anzeigen;
die Portalregelung verwendet nur gültige Regelfelder bis 60 Sekunden Alter.

## Sicherheitsgrenze

Die Messung muss vor Ort durch eine befähigte Person (Elektriker) geprüft sein.
Die Zentrale bestätigt im Portal Sensorzuordnung, Messumfang und Zeitbasis
und klickt „Als geprüfte Quelle freigeben“; Lastwechsel im Portal sind optional.
Bei Gesamtbezug inklusive Ladung dürfen `grid_power_entity` und
`building_power_entity` denselben Sensor verwenden; drei Phasen brauchen
getrennte Phasensensoren.
Quellenfreigabe und Automatik bleiben eigene Vorgänge.

## Update erkennen

Version **0.4.0** meldet im Portal den Agenten
**0.7.0-ha-m13-field-meta-v1**; beim Update bleibt die gespeicherte
Geräteidentität erhalten und eine erneute Kopplung ist nicht erforderlich.
Die Verteilung erfolgt erst nach einer gesonderten Veröffentlichung.
