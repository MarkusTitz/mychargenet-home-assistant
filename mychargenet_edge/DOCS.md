# mychargenet Edge – Home-Assistant-Pilot

Diese Pilot-App liest ausschließlich die ausdrücklich eingetragenen
Home-Assistant-Leistungssensoren und sendet sie ausgehend per HTTPS an
`api.mychargenet.com`. Sie führt keine Home-Assistant-Aktion, keinen
OCPP-Befehl und keine Lastmanagement-Freigabe aus.

Die Zieladresse ist fest an die mychargenet-API gebunden und kann in der
App-Konfiguration nicht auf einen fremden Server umgestellt werden.

## Einrichtung

1. In mychargenet beim richtigen Kunden und Standort ein lokales
   Energie-Gateway vom Typ `Home Assistant` anlegen.
2. Den einmalig angezeigten Kopplungscode innerhalb von 15 Minuten in
   `pairing_code` eintragen.
3. Mindestens eine Entity-ID eintragen. Für die sichere Inbetriebnahme wird
   zuerst `grid_power_entity`, beispielsweise `sensor.grid_power`, empfohlen.
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

Der zentrale Haus- oder Netzmesswert muss weiterhin innerhalb von 120 Sekunden
aktualisiert sein. Home Assistant aktualisiert den Zeitstempel eines
Phasenwerts jedoch teilweise erst, wenn sich dessen Zahlenwert ändert. Deshalb
dürfen unveränderte Phasenwerte standardmäßig bis zu 30 Minuten gehalten
werden, solange die zentrale Messung aktuell bleibt. Das lockert nicht die
Aktualitätsprüfung der primären Regelgröße.

## Sicherheitsgrenze

Diese Pilotversion ist nur zur beobachteten Messwertprüfung vorgesehen. Eine
Messquelle wird nicht automatisch zur Regelquelle. Vor produktivem
Lastmanagement müssen Entity, Einheit, Vorzeichen, Aktualität und mehrere
Laststufen vor Ort geprüft und separat freigegeben werden.
