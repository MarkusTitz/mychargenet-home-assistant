# Änderungsprotokoll

## 0.2.5-pilot

- Das offizielle blaue mychargenet-App-Icon wird jetzt auch im
  Home-Assistant-App-Store und in der App-Detailansicht verwendet.

## 0.2.4-pilot

- Ein älterer, unveränderter Phasenwert macht die frische Haus- oder
  Netzmessung nicht mehr künstlich veraltet.
- Die Aktualität des Datensatzes richtet sich nach der primären Messgröße;
  Phasenwerte bleiben klar getrennte Zusatzdaten.

## 0.2.3-pilot

- Unveränderte Phasenwerte bleiben bis zu 30 Minuten nutzbar, wenn Home
  Assistant ihren Zeitstempel nicht erneuert.
- Der zentrale Haus- oder Netzmesswert bleibt weiterhin auf maximal 120
  Sekunden begrenzt.
- Die getrennte Frist ist in den App-Einstellungen konfigurierbar.

## 0.2.2-pilot

- Öffentlicher Home-Assistant-Installations- und Updatekanal.
- Haus-, Netz-, PV- und Batterieleistung.
- Leistung, Spannung und Strom je Phase.
- Stabile Sicherheitskennung: optionale Diagnosewerte und App-Updates erfordern
  keine erneute Zähler-Inbetriebnahme.
- Standortgebundene Einmalkopplung mit mychargenet.
